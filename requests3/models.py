# -*- coding: utf-8 -*-
"""
requests.models
~~~~~~~~~~~~~~~

This module contains the primary objects that power Requests.
"""

import collections
import datetime
import codecs
import sys

# Import encoding now, to avoid implicit import later.
# Implicit import within threads may cause LookupError when standard library is in a ZIP,
# such as in Embedded Python. See https://github.com/requests/requests/issues/3578.
import rfc3986
import encodings.idna

from urllib3.fields import RequestField
from urllib3.filepost import encode_multipart_formdata
from urllib3.exceptions import (
    DecodeError, ReadTimeoutError, ProtocolError, LocationParseError
)

from io import UnsupportedOperation
from .hooks import default_hooks
from .structures import CaseInsensitiveDict

import requests3 as requests
from .auth import HTTPBasicAuth
from .cookies import cookiejar_from_dict, get_cookie_header, _copy_cookie_jar
from .exceptions import (
    HTTPError,
    MissingScheme,
    InvalidURL,
    ChunkedEncodingError,
    ContentDecodingError,
    ConnectionError,
    StreamConsumedError,
    InvalidHeader,
    InvalidBodyError,
    ReadTimeout,
)
from ._internal_utils import to_native_string, unicode_is_ascii
from .utils import (
    guess_filename,
    get_auth_from_url,
    requote_uri,
    stream_decode_response_unicode,
    to_key_val_list,
    parse_header_links,
    iter_slices,
    guess_json_utf,
    super_len,
    check_header_validity,
    is_stream,
)
from .basics import (
    cookielib,
    urlunparse,
    urlsplit,
    urlencode,
    str,
    bytes,
    chardet,
    builtin_str,
    basestring,
)
import json as complexjson
from .status_codes import codes

# : The set of HTTP status codes that indicate an automatically
#: processable redirect.
REDIRECT_STATI = (
    codes['moved'],  # 301
    codes['found'],  # 302
    codes['other'],  # 303
    codes['temporary_redirect'],  # 307
    codes['permanent_redirect'],  # 308
)
DEFAULT_REDIRECT_LIMIT = 30
CONTENT_CHUNK_SIZE = 10 * 1024
ITER_CHUNK_SIZE = 512


class RequestEncodingMixin(object):

    @property
    def path_url(self):
        """Build the path URL to use."""
        url = []
        p = urlsplit(self.url)
        path = p.path
        if not path:
            path = '/'
        url.append(path)
        query = p.query
        if query:
            url.append('?')
            url.append(query)
        return ''.join(url)

    @staticmethod
    def _encode_params(data):
        """Encode parameters in a piece of data.

        Will successfully encode parameters when passed as a dict or a list of
        2-tuples. Order is retained if data is a list of 2-tuples but arbitrary
        if parameters are supplied as a dict.
        """
        if isinstance(data, (str, bytes)):
            return data

        elif hasattr(data, 'read'):
            return data

        elif hasattr(data, '__iter__'):
            result = []
            for k, vs in to_key_val_list(data):
                if isinstance(vs, basestring) or not hasattr(vs, '__iter__'):
                    vs = [vs]
                for v in vs:
                    if v is not None:
                        result.append(
                            (
                                k.encode('utf-8') if isinstance(k, str) else k,
                                v.encode('utf-8') if isinstance(v, str) else v,
                            )
                        )
            return urlencode(result, doseq=True)

        else:
            return data

    @staticmethod
    def _encode_files(files, data):
        """Build the body for a multipart/form-data request.

        Will successfully encode files when passed as a dict or a list of
        tuples. Order is retained if data is a list of tuples but arbitrary
        if parameters are supplied as a dict.
        The tuples may be 2-tuples (filename, fileobj), 3-tuples (filename, fileobj, contentype)
        or 4-tuples (filename, fileobj, contentype, custom_headers).
        """
        if (not files):
            raise ValueError("Files must be provided.")

        elif isinstance(data, basestring):
            raise ValueError("Data must not be a string.")

        new_fields = []
        fields = to_key_val_list(data or {})
        files = to_key_val_list(files or {})
        for field, val in fields:
            if isinstance(val, basestring) or not hasattr(val, '__iter__'):
                val = [val]
            for v in val:
                if v is not None:
                    # Don't call str() on bytestrings: in Py3 it all goes wrong.
                    if not isinstance(v, bytes):
                        v = str(v)
                    new_fields.append(
                        (
                            field.decode('utf-8') if isinstance(
                                field, bytes
                            ) else field,
                            v.encode('utf-8') if isinstance(v, str) else v,
                        )
                    )
        for (k, v) in files:
            # support for explicit filename
            ft = None
            fh = None
            if isinstance(v, (tuple, list)):
                if len(v) == 2:
                    fn, fp = v
                elif len(v) == 3:
                    fn, fp, ft = v
                else:
                    fn, fp, ft, fh = v
            else:
                fn = guess_filename(v) or k
                fp = v
            if isinstance(fp, (str, bytes, bytearray)):
                fdata = fp
            else:
                fdata = fp.read()
            rf = RequestField(name=k, data=fdata, filename=fn, headers=fh)
            rf.make_multipart(content_type=ft)
            new_fields.append(rf)
        body, content_type = encode_multipart_formdata(new_fields)
        return body, content_type


class RequestHooksMixin(object):

    def register_hook(self, event, hook):
        """Properly register a hook."""
        if event not in self.hooks:
            raise ValueError(
                'Unsupported event specified, with event name "%s"' % (event)
            )

        if isinstance(hook, collections.Callable):
            self.hooks[event].append(hook)
        elif hasattr(hook, '__iter__'):
            self.hooks[event].extend(
                h for h in hook if isinstance(h, collections.Callable)
            )

    def deregister_hook(self, event, hook):
        """Deregister a previously registered hook.
        Returns True if the hook existed, False if not.
        """
        try:
            self.hooks[event].remove(hook)
            return True

        except ValueError:
            return False


class Request(RequestHooksMixin):
    """A user-created :class:`Request <Request>` object.

    Used to prepare a :class:`PreparedRequest <PreparedRequest>`, which is sent to the server.

    :param method: HTTP method to use.
    :param url: URL to send.
    :param headers: dictionary of headers to send.
    :param files: dictionary of {filename: fileobject} files to multipart upload.
    :param data: the body to attach to the request. If a dictionary is provided, form-encoding will take place.
    :param json: json for the body to attach to the request (if files or data is not specified).
    :param params: dictionary of URL parameters to append to the URL.
    :param auth: Auth handler or (user, pass) tuple.
    :param cookies: dictionary or CookieJar of cookies to attach to this request.
    :param hooks: dictionary of callback hooks, for internal usage.

    Usage::

      >>> import requests
      >>> req = requests.Request('GET', 'http://httpbin.org/get')
      >>> req.prepare()
      <PreparedRequest [GET]>
    """
    __slots__ = (
        'method',
        'url',
        'headers',
        'files',
        'data',
        'params',
        'auth',
        'cookies',
        'hooks',
        'json',
    )

    def __init__(
        self,
        method=None,
        url=None,
        headers=None,
        files=None,
        data=None,
        params=None,
        auth=None,
        cookies=None,
        hooks=None,
        json=None,
    ):
        # Default empty dicts for dict params.
        data = [] if data is None else data
        files = [] if files is None else files
        headers = {} if headers is None else headers
        params = {} if params is None else params
        hooks = {} if hooks is None else hooks
        self.hooks = default_hooks()
        for (k, v) in list(hooks.items()):
            self.register_hook(event=k, hook=v)
        self.method = method
        self.url = url
        self.headers = headers
        self.files = files
        self.data = data
        self.json = json
        self.params = params
        self.auth = auth
        self.cookies = cookies

    def __repr__(self):
        return '<Request [%s]>' % (self.method)

    def prepare(self):
        """Constructs a :class:`PreparedRequest <PreparedRequest>` for transmission and returns it."""
        p = PreparedRequest()
        p.prepare(
            method=self.method,
            url=self.url,
            headers=self.headers,
            files=self.files,
            data=self.data,
            json=self.json,
            params=self.params,
            auth=self.auth,
            cookies=self.cookies,
            hooks=self.hooks,
        )
        return p


class PreparedRequest(RequestEncodingMixin, RequestHooksMixin):
    """The fully mutable :class:`PreparedRequest <PreparedRequest>` object,
    containing the exact bytes that will be sent to the server.

    Generated from either a :class:`Request <Request>` object or manually.

    Usage::

      >>> import requests
      >>> req = requests.Request('GET', 'http://httpbin.org/get')
      >>> r = req.prepare()
      <PreparedRequest [GET]>

      >>> s = requests.Session()
      >>> s.send(r)
      <Response [200]>
    """
    __slots__ = (
        'method',
        'url',
        'headers',
        '_cookies',
        'body',
        'hooks',
        '_body_position',
    )

    def __init__(self):
        # : HTTP verb to send to the server.
        self.method = None
        # : HTTP URL to send the request to.
        self.url = None
        # : dictionary of HTTP headers.
        self.headers = None
        # The `CookieJar` used to create the Cookie header will be stored here
        # after prepare_cookies is called
        self._cookies = None
        # : request body to send to the server.
        self.body = None
        # : dictionary of callback hooks, for internal usage.
        self.hooks = default_hooks()
        # : integer denoting starting position of a readable file-like body.
        self._body_position = None

    def prepare(
        self,
        method=None,
        url=None,
        headers=None,
        files=None,
        data=None,
        params=None,
        auth=None,
        cookies=None,
        hooks=None,
        json=None,
    ):
        """Prepares the entire request with the given parameters."""
        self.prepare_method(method)
        self.prepare_url(url, params)
        self.prepare_headers(headers)
        self.prepare_cookies(cookies)
        self.prepare_body(data, files, json)
        self.prepare_auth(auth, url)
        # Note that prepare_auth must be last to enable authentication schemes
        # such as OAuth to work on a fully prepared request.
        # This MUST go after prepare_auth. Authenticators could add a hook
        self.prepare_hooks(hooks)

    def __repr__(self):
        return f'<PreparedRequest [{self.method}]>'

    def copy(self):
        p = PreparedRequest()
        p.method = self.method
        p.url = self.url
        p.headers = self.headers.copy() if self.headers is not None else None
        p._cookies = _copy_cookie_jar(self._cookies)
        p.body = self.body
        p.hooks = self.hooks
        p._body_position = self._body_position
        return p

    def prepare_method(self, method):
        """Prepares the given HTTP method."""
        self.method = method
        if self.method is None:
            raise ValueError('Request method cannot be "None"')

        self.method = to_native_string(self.method.upper())

    @staticmethod
    def _get_idna_encoded_host(host):
        import idna

        try:
            host = idna.encode(host, uts46=True).decode('utf-8')
        except idna.IDNAError:
            raise UnicodeError

        return host

    def prepare_url(self, url, params, validate=False):
        """Prepares the given HTTP URL."""
        # : Accept objects that have string representations.
        #: We're unable to blindly call unicode/str functions
        #: as this will include the bytestring indicator (b'')
        #: on python 3.x.
        #: https://github.com/requests/requests/pull/2238
        if isinstance(url, bytes):
            url = url.decode('utf8')
        else:
            url = str(url)
        # Ignore any leading and trailing whitespace characters.
        url = url.strip()
        # Don't do any URL preparation for non-HTTP schemes like `mailto`,
        # `data` etc to work around exceptions from `url_parse`, which
        # handles RFC 3986 only.
        if ':' in url and not url.lower().startswith('http'):
            self.url = url
            return

        # Support for unicode domain names and paths.
        try:
            uri = rfc3986.urlparse(url)
            if validate:
                rfc3986.normalize_uri(url)
        except rfc3986.exceptions.RFC3986Exception:
            raise InvalidURL(f"Invalid URL {url!r}: URL is imporoper.")

        if not uri.scheme:
            error = (
                "Invalid URL {0!r}: No scheme supplied. Perhaps you meant http://{0}?"
            )
            error = error.format(to_native_string(url, 'utf8'))
            raise MissingScheme(error)

        if not uri.host:
            raise InvalidURL(f"Invalid URL {url!r}: No host supplied")

        # In general, we want to try IDNA encoding the hostname if the string contains
        # non-ASCII characters. This allows users to automatically get the correct IDNA
        # behaviour. For strings containing only ASCII characters, we need to also verify
        # it doesn't start with a wildcard (*), before allowing the unencoded hostname.
        if not unicode_is_ascii(uri.host):
            try:
                uri = uri.copy_with(host=self._get_idna_encoded_host(uri.host))
            except UnicodeError:
                raise InvalidURL('URL has an invalid label.')

        elif uri.host.startswith(u'*'):
            raise InvalidURL('URL has an invalid label.')

        # Bare domains aren't valid URLs.
        if not uri.path:
            uri = uri.copy_with(path='/')
        if isinstance(params, (str, bytes)):
            params = to_native_string(params)
        enc_params = self._encode_params(params)
        if enc_params:
            if uri.query:
                uri = uri.copy_with(query=f'{uri.query}&{enc_params}')
            else:
                uri = uri.copy_with(query=enc_params)
        # url = requote_uri(
        #     urlunparse([uri.scheme, uri.authority, uri.path, None, uri.query, uri.fragment])
        # )
        # Normalize the URI.
        self.url = rfc3986.normalize_uri(uri.unsplit())

    def prepare_headers(self, headers):
        """Prepares the given HTTP headers."""
        self.headers = CaseInsensitiveDict()
        if headers:
            for header in headers.items():
                # Raise exception on invalid header value.
                check_header_validity(header)
                name, value = header
                self.headers[to_native_string(name)] = value

    def prepare_body(self, data, files, json=None):
        """Prepares the given HTTP body data."""
        # Check if file, fo, generator, iterator.
        # If not, run through normal process.
        # Nottin' on you.
        body = None
        content_type = None
        if not data and json is not None:
            # urllib3 requires a bytes-like body. Python 2's json.dumps
            # provides this natively, but Python 3 gives a Unicode string.
            content_type = 'application/json'
            body = complexjson.dumps(json)
            if not isinstance(body, bytes):
                body = body.encode('utf-8')
        if is_stream(data):
            body = data
            if getattr(body, 'tell', None) is not None:
                # Record the current file position before reading.
                # This will allow us to rewind a file in the event
                # of a redirect.
                try:
                    self._body_position = body.tell()
                except (IOError, OSError):
                    # This differentiates from None, allowing us to catch
                    # a failed `tell()` later when trying to rewind the body
                    self._body_position = object()
            if files:
                raise NotImplementedError(
                    'Streamed bodies and files are mutually exclusive.'
                )

        else:
            # Multi-part file uploads.
            if files:
                (body, content_type) = self._encode_files(files, data)
            else:
                if data:
                    body = self._encode_params(data)
                    if isinstance(data, basestring) or hasattr(data, 'read'):
                        content_type = None
                    else:
                        content_type = 'application/x-www-form-urlencoded'
            # Add content-type if it wasn't explicitly provided.
            if content_type and ('content-type' not in self.headers):
                self.headers['Content-Type'] = content_type
        self.prepare_content_length(body)
        self.body = body

    def prepare_content_length(self, body):
        """Prepares Content-Length header.

        If the length of the body of the request can be computed, Content-Length
        is set using ``super_len``. If user has manually set either a
        Transfer-Encoding or Content-Length header when it should not be set
        (they should be mutually exclusive) an InvalidHeader
        error will be raised.
        """
        if body is not None:
            length = super_len(body)
            if length:
                self.headers['Content-Length'] = builtin_str(length)
            elif is_stream(body):
                self.headers['Transfer-Encoding'] = 'chunked'
            else:
                raise InvalidBodyError(
                    'Non-null body must have length or be streamable.'
                )

        elif self.method not in ('GET', 'HEAD') and self.headers.get(
            'Content-Length'
        ) is None:
            # Set Content-Length to 0 for methods that can have a body
            # but don't provide one. (i.e. not GET or HEAD)
            self.headers['Content-Length'] = '0'
        if 'Transfer-Encoding' in self.headers and 'Content-Length' in self.headers:
            raise InvalidHeader(
                'Conflicting Headers: Both Transfer-Encoding and '
                'Content-Length are set.'
            )

    def prepare_auth(self, auth, url=''):
        """Prepares the given HTTP auth data."""
        # If no Auth is explicitly provided, extract it from the URL first.
        if auth is None:
            url_auth = get_auth_from_url(self.url)
            auth = url_auth if any(url_auth) else None
        if auth:
            if isinstance(auth, tuple) and len(auth) == 2:
                # special-case basic HTTP auth
                auth = HTTPBasicAuth(*auth)
            # Allow auth to make its changes.
            r = auth(self)
            # Update self to reflect the auth changes.
            self.__dict__.update(r.__dict__)
            # Recompute Content-Length
            self.prepare_content_length(self.body)

    def prepare_cookies(self, cookies):
        """Prepares the given HTTP cookie data.

        This function eventually generates a ``Cookie`` header from the
        given cookies using cookielib. Due to cookielib's design, the header
        will not be regenerated if it already exists, meaning this function
        can only be called once for the life of the
        :class:`PreparedRequest <PreparedRequest>` object. Any subsequent calls
        to ``prepare_cookies`` will have no actual effect, unless the "Cookie"
        header is removed beforehand.
        """
        if isinstance(cookies, cookielib.CookieJar):
            self._cookies = cookies
        else:
            self._cookies = cookiejar_from_dict(cookies)
        cookie_header = get_cookie_header(self._cookies, self)
        if cookie_header is not None:
            self.headers['Cookie'] = cookie_header

    def prepare_hooks(self, hooks):
        """Prepares the given hooks."""
        # hooks can be passed as None to the prepare method and to this
        # method. To prevent iterating over None, simply use an empty list
        # if hooks is False-y
        hooks = hooks or []
        for event in hooks:
            self.register_hook(event, hooks[event])

    def send(self, session=None, **send_kwargs):
        """Sends the PreparedRequest to the given Session.
        If none is provided, one is created for you."""
        session = requests.Session() if session is None else session
        with session:
            return session.send(self, **send_kwargs)


class Response(object):
    """The :class:`Response <Response>` object, which contains a
    server's response to an HTTP request.
    """
    __attrs__ = [
        '_content',
        'status_code',
        'headers',
        'url',
        'history',
        'encoding',
        'reason',
        'cookies',
        'elapsed',
        'request',
    ]
    __slots__ = __attrs__ + ['_content_consumed', 'raw', '_next', 'connection']

    def __init__(self):
        self._content = False
        self._content_consumed = False
        self._next = None
        # : Integer Code of responded HTTP Status, e.g. 404 or 200.
        self.status_code = None
        # : Case-insensitive Dictionary of Response Headers.
        #: For example, ``headers['content-encoding']`` will return the
        #: value of a ``'Content-Encoding'`` response header.
        self.headers = CaseInsensitiveDict()
        # : File-like object representation of response (for advanced usage).
        #: Use of ``raw`` requires that ``stream=True`` be set on the request.
        # This requirement does not apply for use internally to Requests.
        self.raw = None
        # : Final URL location of Response.
        self.url = None
        # : Encoding to decode with when accessing r.text or
        #: r.iter_content(decode_unicode=True)
        self.encoding = None
        # : A list of :class:`Response <Response>` objects from
        #: the history of the Request. Any redirect responses will end
        #: up here. The list is sorted from the oldest to the most recent request.
        self.history = []
        # : Textual reason of responded HTTP Status, e.g. "Not Found" or "OK".
        self.reason = None
        # : A CookieJar of Cookies the server sent back.
        self.cookies = cookiejar_from_dict({})
        # : The amount of time elapsed between sending the request
        #: and the arrival of the response (as a timedelta).
        #: This property specifically measures the time taken between sending
        #: the first byte of the request and finishing parsing the headers. It
        #: is therefore unaffected by consuming the response content or the
        #: value of the ``stream`` keyword argument.
        self.elapsed = datetime.timedelta(0)
        # : The :class:`PreparedRequest <PreparedRequest>` object to which this
        #: is a response.
        self.request = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __getstate__(self):
        # Consume everything; accessing the content attribute makes
        # sure the content has been fully read.
        if not self._content_consumed:
            self.content
        return {attr: getattr(self, attr, None) for attr in self.__attrs__}

    def __setstate__(self, state):
        for name, value in state.items():
            setattr(self, name, value)
        # pickled objects do not have .raw
        setattr(self, '_content_consumed', True)
        setattr(self, 'raw', None)

    def __repr__(self):
        return '<Response [%s]>' % (self.status_code)

    def __iter__(self):
        """Allows you to use a response as an iterator."""
        return self.iter_content(128)

    @property
    def ok(self):
        """Returns True if :attr:`status_code` is less than 400.

        This attribute checks if the status code of the response is between
        400 and 600 to see if there was a client error or a server error. If
        the status code, is between 200 and 400, this will return True. This
        is **not** a check to see if the response code is ``200 OK``.
        """
        try:
            self.raise_for_status()
        except HTTPError:
            return False

        return True

    @property
    def is_redirect(self):
        """True if this Response is a well-formed HTTP redirect that could have
        been processed automatically (by :meth:`Session.resolve_redirects`).
        """
        return (
            'location' in self.headers and self.status_code in REDIRECT_STATI
        )

    @property
    def is_permanent_redirect(self):
        """True if this Response one of the permanent versions of redirect."""
        return (
            'location' in self.headers and
            self.status_code in (
                codes.moved_permanently, codes.permanent_redirect
            )
        )

    @property
    def next(self):
        """Returns a PreparedRequest for the next request in a redirect chain, if there is one."""
        return self._next

    @property
    def apparent_encoding(self):
        """The apparent encoding, provided by the chardet library."""
        return chardet.detect(self.content)['encoding']

    def iter_content(self, decode_unicode=False):
        """Iterates over the response data.  When stream=True is set on the
        request, this avoids reading the content at once into memory for
        large responses.  The chunk size is the number of bytes it should
        read into memory.  This is not necessarily the length of each item
        returned as decoding can take place.

        chunk_size must be of type int or None. A value of None will
        function differently depending on the value of `stream`.
        stream=True will read data as it arrives in whatever size the
        chunks are received. If stream=False, data is returned as
        a single chunk.

        If using decode_unicode, the encoding must be set to a valid encoding
        enumeration before invoking iter_content.
        """

        DEFAULT_CHUNK_SIZE = 1

        def generate():
            # Special case for urllib3.
            if hasattr(self.raw, 'stream'):
                try:
                    for chunk in self.raw.stream(
                        # chunk_size, decode_content=True
                        decode_content=True
                    ):
                        yield chunk

                except ProtocolError as e:
                    if self.headers.get('Transfer-Encoding') == 'chunked':
                        raise ChunkedEncodingError(e)

                    else:
                        raise ConnectionError(e)

                except DecodeError as e:
                    raise ContentDecodingError(e)

                except ReadTimeoutError as e:
                    raise ReadTimeout(e)

            else:
                # Standard file-like object.
                while True:
                    chunk = self.raw.read(chunk_size)
                    if not chunk:
                        break

                    yield chunk

            self._content_consumed = True

        if self._content_consumed and isinstance(self._content, bool):
            raise StreamConsumedError()

        # elif chunk_size is not None and not isinstance(chunk_size, int):
        #     raise TypeError(
        #         f"chunk_size must be an int, it is instead a {type(chunk_size)}."
        #     )

        # simulate reading small chunks of the content
        reused_chunks = iter_slices(self._content, DEFAULT_CHUNK_SIZE)
        stream_chunks = generate()

        chunks = reused_chunks if self._content_consumed else stream_chunks
        if decode_unicode:
            if self.encoding is None:
                raise TypeError(
                    'encoding must be set before consuming streaming '
                    'responses'
                )

            # check encoding value here, don't wait for the generator to be
            # consumed before raising an exception
            codecs.lookup(self.encoding)
            chunks = stream_decode_response_unicode(chunks, self)
        return chunks

    def iter_lines(
        self, chunk_size=ITER_CHUNK_SIZE, decode_unicode=None, delimiter=None
    ):
        """Iterates over the response data, one line at a time.  When
        stream=True is set on the request, this avoids reading the
        content at once into memory for large responses.

        .. note:: This method is not reentrant safe.
        """
        carriage_return = u'\r' if decode_unicode else b'\r'
        line_feed = u'\n' if decode_unicode else b'\n'
        pending = None
        last_chunk_ends_with_cr = False
        for chunk in self.iter_content(
            chunk_size=chunk_size, decode_unicode=decode_unicode
        ):
            # Skip any null responses: if there is pending data it is necessarily an
            # incomplete chunk, so if we don't have more data we don't want to bother
            # trying to get it. Unconsumed pending data will be yielded anyway in the
            # end of the loop if the stream ends.
            if not chunk:
                continue

            # Consume any pending data
            if pending is not None:
                chunk = pending + chunk
                pending = None
            # Either split on a line, or split on a specified delimiter
            if delimiter:
                lines = chunk.split(delimiter)
            else:
                # Python splitlines() supports the universal newline (PEP 278).
                # That means, '\r', '\n', and '\r\n' are all treated as end of
                # line. If the last chunk ends with '\r', and the current chunk
                # starts with '\n', they should be merged and treated as only
                # *one* new line separator '\r\n' by splitlines().
                # This rule only applies when splitlines() is used.
                # The last chunk ends with '\r', so the '\n' at chunk[0]
                # is just the second half of a '\r\n' pair rather than a
                # new line break. Just skip it.
                skip_first_char = last_chunk_ends_with_cr and chunk.startswith(
                    line_feed
                )
                last_chunk_ends_with_cr = chunk.endswith(carriage_return)
                if skip_first_char:
                    chunk = chunk[1:]
                    # it's possible that after stripping the '\n' then chunk becomes empty
                    if not chunk:
                        continue

                lines = chunk.splitlines()
            # Calling `.split(delimiter)` will always end with whatever text
            # remains beyond the delimiter, or '' if the delimiter is the end
            # of the text.  On the other hand, `.splitlines()` doesn't include
            # a '' if the text ends in a line delimiter.
            #
            # For example:
            #
            #     'abc\ndef\n'.split('\n')  ~> ['abc', 'def', '']
            #     'abc\ndef\n'.splitlines() ~> ['abc', 'def']
            #
            # So if we have a specified delimiter, we always pop the final
            # item and prepend it to the next chunk.
            #
            # If we're using `splitlines()`, we only do this if the chunk
            # ended midway through a line.
            incomplete_line = lines[-1] and lines[-1][-1] == chunk[-1]
            if delimiter or incomplete_line:
                pending = lines.pop()
            for line in lines:
                yield line

        if pending is not None:
            yield pending

    @property
    def content(self):
        """Content of the response, in bytes."""
        if self._content is False:
            # Read the contents.
            if self._content_consumed:
                raise RuntimeError(
                    'The content for this response was already consumed'
                )

            if self.status_code == 0 or self.raw is None:
                self._content = None
            else:
                # self._content = await self.iter_content(CONTENT_CHUNK_SIZE)
                # print(bytes().join(
                #     [await self.iter_content(CONTENT_CHUNK_SIZE)]
                # ))
                self._content = bytes().join(
                    self.iter_content()
                ) or bytes()
        self._content_consumed = True
        # don't need to release the connection; that's been handled by urllib3
        # since we exhausted the data.
        return self._content

    @property
    def text(self):
        """Content of the response, in unicode.

        If Response.encoding is None, encoding will be guessed using
        ``chardet``.

        The encoding of the response content is determined based solely on HTTP
        headers, following RFC 2616 to the letter. If you can take advantage of
        non-HTTP knowledge to make a better guess at the encoding, you should
        set ``r.encoding`` appropriately before accessing this property.
        """
        # Try charset from content-type
        content = None
        encoding = self.encoding
        if not self.content:
            return str('')

        # Fallback to auto-detected encoding.
        if self.encoding is None:
            encoding = self.apparent_encoding
        # Decode unicode from given encoding.
        try:
            content = str(self.content, encoding, errors='replace')
        except (LookupError, TypeError):
            # A LookupError is raised if the encoding was not found which could
            # indicate a misspelling or similar mistake.
            #
            # A TypeError can be raised if encoding is None
            #
            # So we try blindly encoding.
            content = str(self.content, errors='replace')
        return content

    def json(self, **kwargs):
        r"""Returns the json-encoded content of a response, if any.

        :param \*\*kwargs: Optional arguments that ``json.loads`` takes.
        :raises ValueError: If the response body does not contain valid json.
        """
        if not self.encoding and self.content and len(self.content) > 3:
            # No encoding set. JSON RFC 4627 section 3 states we should expect
            # UTF-8, -16 or -32. Detect which one to use; If the detection or
            # decoding fails, fall back to `self.text` (using chardet to make
            # a best guess).
            encoding = guess_json_utf(self.content)
            if encoding is not None:
                try:
                    content = self.content
                    return complexjson.loads(
                        content.decode(encoding), **kwargs
                    )

                except UnicodeDecodeError:
                    # Wrong UTF codec detected; usually because it's not UTF-8
                    # but some other 8-bit codec.  This is an RFC violation,
                    # and the server didn't bother to tell us what codec *was*
                    # used.
                    pass
        return complexjson.loads(self.text, **kwargs)

    @property
    def links(self):
        """Returns the parsed header links of the response, if any."""
        header = self.headers.get('link')
        # l = MultiDict()
        l = {}
        if header:
            links = parse_header_links(header)
            for link in links:
                key = link.get('rel') or link.get('url')
                l[key] = link
        return l

    def raise_for_status(self):
        """Raises stored :class:`HTTPError`, if one occurred.
        Otherwise, returns the response object (self)."""
        http_error_msg = ''
        if isinstance(self.reason, bytes):
            # We attempt to decode utf-8 first because some servers
            # choose to localize their reason strings. If the string
            # isn't utf-8, we fall back to iso-8859-1 for all other
            # encodings. (See PR #3538)
            try:
                reason = self.reason.decode('utf-8')
            except UnicodeDecodeError:
                reason = self.reason.decode('iso-8859-1')
        else:
            reason = self.reason
        if 400 <= self.status_code < 500:
            http_error_msg = u'%s Client Error: %s for url: %s' % (
                self.status_code, reason, self.url
            )
        elif 500 <= self.status_code < 600:
            http_error_msg = u'%s Server Error: %s for url: %s' % (
                self.status_code, reason, self.url
            )
        if http_error_msg:
            raise HTTPError(http_error_msg, response=self)

        return self

    def close(self):
        """Releases the connection back to the pool. Once this method has been
        called the underlying ``raw`` object must not be accessed again.

        *Note: Should not normally need to be called explicitly.*
        """
        if not self._content_consumed:
            self.raw.close()
        release_conn = getattr(self.raw, 'release_conn', None)
        if release_conn is not None:
            release_conn()


class AsyncResponse(Response):
    def __init__(self, *args, **kwargs):
        super(AsyncResponse, self).__init__(*args, **kwargs)

    async def json(self, **kwargs):
        r"""Returns the json-encoded content of a response, if any.

        :param \*\*kwargs: Optional arguments that ``json.loads`` takes.
        :raises ValueError: If the response body does not contain valid json.
        """
        if not self.encoding and await self.content and len(await self.content) > 3:
            # No encoding set. JSON RFC 4627 section 3 states we should expect
            # UTF-8, -16 or -32. Detect which one to use; If the detection or
            # decoding fails, fall back to `self.text` (using chardet to make
            # a best guess).
            encoding = guess_json_utf(await self.content)
            if encoding is not None:
                try:
                    content = await self.content
                    return complexjson.loads(
                        content.decode(encoding), **kwargs
                    )

                except UnicodeDecodeError:
                    # Wrong UTF codec detected; usually because it's not UTF-8
                    # but some other 8-bit codec.  This is an RFC violation,
                    # and the server didn't bother to tell us what codec *was*
                    # used.
                    pass
        return complexjson.loads(await self.text, **kwargs)

    @property
    async def text(self):
        """Content of the response, in unicode.

        If Response.encoding is None, encoding will be guessed using
        ``chardet``.

        The encoding of the response content is determined based solely on HTTP
        headers, following RFC 2616 to the letter. If you can take advantage of
        non-HTTP knowledge to make a better guess at the encoding, you should
        set ``r.encoding`` appropriately before accessing this property.
        """
        # Try charset from content-type
        content = None
        encoding = self.encoding
        if not await self.content:
            return str('')

        # Fallback to auto-detected encoding.
        if self.encoding is None:
            encoding = self.apparent_encoding
        # Decode unicode from given encoding.
        try:
            content = str(self.content, encoding, errors='replace')
        except (LookupError, TypeError):
            # A LookupError is raised if the encoding was not found which could
            # indicate a misspelling or similar mistake.
            #
            # A TypeError can be raised if encoding is None
            #
            # So we try blindly encoding.
            content = str(await self.content, errors='replace')
        return content

    @property
    async def content(self):
        """Content of the response, in bytes."""
        if self._content is False:
            # Read the contents.
            if self._content_consumed:
                raise RuntimeError(
                    'The content for this response was already consumed'
                )

            if self.status_code == 0 or self.raw is None:
                self._content = None
            else:
                # self._content = await self.iter_content(CONTENT_CHUNK_SIZE)
                # print(bytes().join(
                #     [await self.iter_content(CONTENT_CHUNK_SIZE)]
                # ))
                self._content = bytes().join(
                    [await self.iter_content()]
                ) or bytes()
        self._content_consumed = True
        # don't need to release the connection; that's been handled by urllib3
        # since we exhausted the data.
        return self._content


    @property
    async def apparent_encoding(self):
        """The apparent encoding, provided by the chardet library."""
        return chardet.detect(await self.content)['encoding']

    async def iter_content(self, decode_unicode=False):
        """Iterates over the response data.  When stream=True is set on the
        request, this avoids reading the content at once into memory for
        large responses.  The chunk size is the number of bytes it should
        read into memory.  This is not necessarily the length of each item
        returned as decoding can take place.

        chunk_size must be of type int or None. A value of None will
        function differently depending on the value of `stream`.
        stream=True will read data as it arrives in whatever size the
        chunks are received. If stream=False, data is returned as
        a single chunk.

        If using decode_unicode, the encoding must be set to a valid encoding
        enumeration before invoking iter_content.
        """

        DEFAULT_CHUNK_SIZE = 1

        async def generate():
            # Special case for urllib3.
            if hasattr(self.raw, 'stream'):
                try:
                    async for chunk in self.raw.stream(
                        # chunk_size, decode_content=True
                        decode_content=True
                    ):
                        yield chunk

                except ProtocolError as e:
                    if self.headers.get('Transfer-Encoding') == 'chunked':
                        raise ChunkedEncodingError(e)

                    else:
                        raise ConnectionError(e)

                except DecodeError as e:
                    raise ContentDecodingError(e)

                except ReadTimeoutError as e:
                    raise ReadTimeout(e)

            else:
                # Standard file-like object.
                while True:
                    chunk = await self.raw.read(chunk_size)
                    if not chunk:
                        break

                    yield chunk

            self._content_consumed = True

        if self._content_consumed and isinstance(self._content, bool):
            raise StreamConsumedError()