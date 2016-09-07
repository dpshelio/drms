# Copyright (c) 2014-2016 Kolja Glogowski
# Copyright (c) 2016 Monica Bobra and Arthur Amezcua
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.

from __future__ import absolute_import, division, print_function
import sys
import re
import os
import time
import warnings
import json
import six
from six.moves.urllib.request import urlopen, urlretrieve
from six.moves.urllib.error import HTTPError, URLError
from six.moves.urllib.parse import urlencode, urljoin, quote_plus
import pandas as pd
import numpy as np


__all__ = [
    'Location', 'register_location', 'const', 'to_datetime', 'DrmsError',
    'DrmsQueryError', 'DrmsExportError', 'SeriesInfo', 'ExportRequest',
    'Client']

__author__ = 'Kolja Glogowski, Monica Bobra, Arthur Amezcua'
__maintainer__ = 'Kolja Glogowski'
__email__ = 'kolja@pixie.de'
__license__ = 'MIT'
__version__ = '0.2.2'


# Compatibility functions for older pandas versions.
if tuple(map(int, pd.__version__.split('.')[:2])) < (0, 17):
    def _pd_to_datetime_coerce(arg):
        return pd.to_datetime(arg, coerce=True)

    def _pd_to_numeric_coerce(arg):
        if not isinstance(arg, pd.Series):
            arg = pd.Series(arg)
        return arg.convert_objects(
            convert_dates=False, convert_numeric=True,
            convert_timedeltas=False)
else:
    def _pd_to_datetime_coerce(arg):
        return pd.to_datetime(arg, errors='coerce')

    def _pd_to_numeric_coerce(arg):
        return pd.to_numeric(arg, errors='coerce')


class Location(object):
    def __init__(self, name, cgi_baseurl,
                 cgi_show_series='show_series',
                 cgi_jsoc_info='jsoc_info',
                 cgi_jsoc_fetch=None,
                 cgi_check_address=None,
                 http_download_baseurl=None,
                 ftp_download_baseurl=None):
        self.name = name
        self.cgi_baseurl = cgi_baseurl
        self.cgi_show_series = cgi_show_series
        if cgi_show_series is not None:
            self.url_show_series = urljoin(cgi_baseurl, cgi_show_series)
        self.cgi_jsoc_info = cgi_jsoc_info
        if cgi_jsoc_info is not None:
            self.url_jsoc_info = urljoin(cgi_baseurl, cgi_jsoc_info)
        self.cgi_jsoc_fetch = cgi_jsoc_fetch
        if cgi_jsoc_fetch is not None:
            self.url_jsoc_fetch = urljoin(cgi_baseurl, cgi_jsoc_fetch)
        self.cgi_check_address = cgi_check_address
        if cgi_check_address is not None:
            self.url_check_address = urljoin(cgi_baseurl, cgi_check_address)
        self.http_download_baseurl = http_download_baseurl
        self.ftp_download_baseurl = ftp_download_baseurl

    def __repr__(self):
        return '<Location "%s">' % self.name


def register_location(loc):
    global _locations
    name = loc.name.lower()
    if name in _locations:
        raise RuntimeError('Location "%s" already registered' % name)
    _locations[loc.name.lower()] = loc


# Register known locations
_locations = {}
register_location(Location(
    name='jsoc',
    cgi_baseurl='http://jsoc.stanford.edu/cgi-bin/ajax/',
    cgi_show_series='show_series',
    cgi_jsoc_info='jsoc_info',
    cgi_jsoc_fetch='jsoc_fetch',
    cgi_check_address='checkAddress.sh',
    http_download_baseurl='http://jsoc.stanford.edu/',
    ftp_download_baseurl='ftp://pail.stanford.edu/export/'))
register_location(Location(
    name='kis',
    cgi_baseurl='http://drms.leibniz-kis.de/cgi-bin/',
    cgi_show_series='show_series',
    cgi_jsoc_info='jsoc_info'))


# Constants for jsoc_info calls
class _JsocInfoConstants:
    all = '**ALL**'
    none = '**NONE**'
    recnum = '*recnum*'
    sunum = '*sunum*'
    size = '*size*'
    online = '*online*'
    retain = '*retain*'
    logdir = '*logdir*'
const = _JsocInfoConstants()


def _split_arg(arg):
    if isinstance(arg, six.string_types):
        arg = [it for it in re.split(r'[\s,]+', arg) if it]
    return arg


def _extract_series_name(ds):
    """Extract series name from record set."""
    m = re.match(r'^\s*([\w\.]+).*$', ds)
    return m.group(1) if m is not None else None


def to_datetime(tstr, force=False):
    """
    Tries to parse JSOC time strings. In general, this is quite complicated
    because of the many different (non-standard) ways they support. For more
    (much more!) details on this matter, see Rick Bogarts notes at

        http://jsoc.stanford.edu/doc/timerep.html

    Here we only try to convert the typical HMI time strings, with a format
    like %Y.%m.%d_%H:%M:%S_TAI, to an ISO time string, that can be parsed by
    pandas. Note that _TAI, aswell as other timezone indentifier like Z,
    will not be taken into account, so the result will be a naive timestamp
    without any associated timezone.

    If you know the time string format, it might be better calling
    pandas.to_datetime() directly.

    Parameters
    ----------
    tstr : string or list/Series containing strings
        DateTime strings.
    force : boolean
        Set to True to omit the endswith('_TAI') check.

    Returns
    -------
    result : Series or Timestamp
        Pandas series or a single Timestamp object
    """
    s = pd.Series(tstr)
    if force or s.str.endswith('_TAI').any():
        s = s.str.replace('_TAI', '')
        s = s.str.replace('_', ' ')
        s = s.str.replace('.', '-', n=2)
    res = _pd_to_datetime_coerce(s)
    return res.iloc[0] if isinstance(tstr, six.string_types) else res


class DrmsError(RuntimeError):
    pass


class DrmsQueryError(DrmsError):
    pass


class DrmsExportError(DrmsError):
    pass


class JsonRequest(object):
    def __init__(self, url, encoding):
        self._encoding = encoding
        self._http = urlopen(url)
        self._data_str = None
        self._data = None

    def __repr__(self):
        return '<JsonRequest "%s">' % self.url

    @property
    def url(self):
        return self._http.url

    @property
    def raw_data(self):
        if self._data_str is None:
            self._data_str = self._http.read()
        return self._data_str

    @property
    def data(self):
        if self._data is None:
            self._data = json.loads(self.raw_data.decode(self._encoding))
        return self._data


class JsonClient(object):
    def __init__(self, location='jsoc', encoding='latin1', debug=False):
        """
        Parameters
        ----------
        location : string or Location
            Registered server location ID or Location instance.
            Defaults to JSOC.
        encoding : string
            Character encoding (default is 'latin1').
        debug : boolean
            Enable or disable debug mode (default is disabled).
        """
        self._encoding = encoding
        if isinstance(location, Location):
            self._loc = location
        else:
            self._loc = _locations[location.lower()]
        self.debug = debug

    def __repr__(self):
        return '<JsonClient "%s">' % self._loc.cgi_baseurl

    def _json_request(self, url):
        if self.debug:
            print(url)
        return JsonRequest(url, self._encoding)

    @property
    def location(self):
        return self._loc

    @property
    def debug(self):
        return self._debug

    @debug.setter
    def debug(self, value):
        self._debug = True if value else False

    def show_series(self, ds_filter=None):
        """
        List available data series.

        Parameters
        ----------
        ds_filter : string
            Name filter regexp.

        Returns
        -------
        result : dict
        """
        query = '?' if ds_filter is not None else ''
        if ds_filter is not None:
            query += urlencode({'filter': ds_filter})
        req = self._json_request(self._loc.url_show_series + query)
        return req.data

    def series_struct(self, ds):
        """
        Get information about the content of a data series.

        Parameters
        ----------
        ds : string
            Name of the data series.

        Returns
        -------
        result : dict
            Dictionary containing information about the data series.
        """
        query = '?' + urlencode({'op': 'series_struct', 'ds': ds})
        req = self._json_request(self._loc.url_jsoc_info + query)
        return req.data

    def export_old(self, ds, requestor, notify):
        """
        This function exports data as FITS files. To do this, the function
        binds metadata (keywords) to images (arrays) to create FITS files and
        then serves the FITS files at jsoc.stanford.edu.
        Written by Monica Bobra and Art Amezcua
        19 July 2016

        Parameters
        ----------
        requestor : string
            Username of requestor.
        notify : string
            E-mail address of requestor.
        ds : string
            Name of the data series.

        Returns
        -------
        supath : list
            List containing paths to all the requested FITS files.
        """
        # test to see if the user's e-mail address is registered with
        # jsoc.stanford.edu
        test_email_query = 'http://jsoc.stanford.edu/cgi-bin/ajax/' \
            + 'checkAddress.sh?address=' + quote_plus(notify) + '&checkonly=1'
        response = urlopen(test_email_query)
        data = json.loads(response.read().decode(self._encoding))
        if (data['status'] == 4):
            raise RuntimeError(
                'User e-mail address is not registered with jsoc.stanford.edu')

        query = '?' + urlencode({
            'op': 'exp_request', 'protocol': 'fits', 'format': 'json',
            'method': 'url', 'requestor': requestor, 'notify': notify,
            'ds': ds})
        req = self._json_request(self._loc.url_jsoc_fetch + query)

        # waiting for the request to be ready
        if (int(req.data['status']) == 1 or int(req.data['status']) == 2):
            if 'requestid' in req.data:
                query = '?' + urlencode({
                    'op': 'exp_status', 'requestid': req.data['requestid']})
                supath = []
                print('Waiting for the request to be ready. Please allow '
                      'at least 20 seconds.')
                time.sleep(15)
                while True:
                    req = self._json_request(self._loc.url_jsoc_fetch + query)
                    if int(req.data['status']) in [1, 2, 6]:
                        time.sleep(5)
                    elif int(req.data['status']) == 0:
                        dir = req.data['dir']
                        for dataobj in req.data['data']:
                            supath.append(urljoin(
                                self._loc.http_download_baseurl,
                                os.path.join(
                                    req.data['dir'], dataobj['filename'])))
                        break
                    else:
                        print(type(req.data['status']))
                        if (req.data['status'] == 3):
                            raise RuntimeError(
                                'DRMS Query failed, request size is too '
                                'large, status=%s' % req.data['status'])
                        if (req.data['status'] == 4):
                            raise RuntimeError(
                                'DRMS Query failed, request not formed '
                                'correctly, status=%s' % req.data['status'])
                        if (req.data['status'] == 5):
                            raise RuntimeError(
                                'DRMS Query failed, export request expired, '
                                'status=%s' % req.data['status'])
            else:
                raise RuntimeError(
                    'DRMS Query failed, there is no requestid, status=%s' %
                    req.data['status'])
        else:
            raise RuntimeError(
                'DRMS Query failed, series is not a valid series, status=%s' %
                req.data['status'])
        print("All the data are available at:")
        print(str(urljoin(self._loc.http_download_baseurl, req.data['dir'])))
        return supath

    def rs_summary(self, ds):
        """
        Get summary (i.e. count) of a given record set.

        Parameters
        ----------
        ds : string
            Record set query (only one series).

        Returns
        -------
        result : dict
            Dictionary containg 'count', 'status' and 'runtime'.
        """
        query = '?' + urlencode({'op': 'rs_summary', 'ds': ds})
        req = self._json_request(self._loc.url_jsoc_info + query)
        return req.data

    def rs_list(self, ds, key=None, seg=None, link=None, uid=None):
        """
        Get detailed information about a record set.

        Parameters
        ----------
        ds : string
            Record set query.
        key : string, list or None
            List of requested keywords, optional.
        seg : string, list or None
            List of requested segments, optional.
        link : string or None
            List of requested Links, optional.
        uid : string or None
            Session ID used when calling rs_list CGI, optional.

        Returns
        -------
        result : dict
            Dictionary containing the requested record set information.
        """
        if key is None and seg is None and link is None:
            raise ValueError('At least one key, seg or link must be specified')
        d = {'op': 'rs_list', 'ds': ds}
        if key is not None:
            d['key'] = ','.join(_split_arg(key))
        if seg is not None:
            d['seg'] = ','.join(_split_arg(seg))
        if link is not None:
            d['link'] = ','.join(_split_arg(link))
        if uid is not None:
            d['userhandle'] = uid
        query = '?' + urlencode(d)
        req = self._json_request(self._loc.url_jsoc_info + query)
        return req.data

    def check_address(self, email):
        """
        Check if an email address is registered for export data requests.

        Parameters
        ----------
        email : string
            Email address to be verified.

        Returns
        -------
        result : dict
            Dictionary containing 'status' and 'msg'. Some status codes are:
                2: Email address is valid and registered
                4: Email address has neither been validated nor registered
               -2: Not a valid email address
        """
        query = '?' + urlencode({
            'address': quote_plus(email), 'checkonly': '1'})
        req = self._json_request(self._loc.url_check_address + query)
        return req.data

    def exp_request(self, ds, notify, method='url_quick', protocol='as-is',
                    protocol_args=None, filenamefmt=None, requestor=None):
        """
        Request data export.

        Parameters
        ----------
        ds : string
        notify : string
        method : string
        protocol : string
        protocol_args : dict or None
        filenamefmt : string, None
        requestor : string, None or False

        Returns
        -------
        result : dict
        """
        method = method.lower()
        method_list = ['url_quick', 'url', 'url-tar', 'ftp', 'ftp-tar']
        if method not in method_list:
            raise ValueError(
                "Method '%s' is not supported, valid methods are: %s" %
                (method, ', '.join("'%s'" % s for s in method_list)))

        protocol = protocol.lower()
        img_protocol_list = ['jpg', 'mpg', 'mp4']
        protocol_list = ['as-is', 'fits'] + img_protocol_list
        if protocol not in protocol_list:
            raise ValueError(
                "Protocol '%s' is not supported, valid protocols are: %s" %
                (protocol, ', '.join("'%s'" % s for s in protocol_list)))

        # method "url_quick" is meant to be used with "as-is", change method
        # to "url" if protocol is not "as-is"
        if method == 'url_quick' and protocol != 'as-is':
            method = 'url'

        if protocol in img_protocol_list:
            d = {'ct': 'grey.sao', 'scaling': 'minmax', 'size': 1}
            if protocol_args is not None:
                for k, v in protocol_args.items():
                    if k.lower() == 'ct':
                        d['ct'] = v
                    elif k == 'scaling':
                        d[k] = v
                    elif k == 'size':
                        d[k] = int(v)
                    elif k in ['min', 'max']:
                        d[k] = float(v)
                    else:
                        raise ValueError("Unknown protocol argument: '%s'" % k)
            protocol += ',CT={ct},scaling={scaling},size={size}'.format(**d)
            if 'min' in d:
                protocol += ',min=%g' % d['min']
            if 'max' in d:
                protocol += ',max=%g' % d['max']
        else:
            if protocol_args is not None:
                raise ValueError(
                    "protocol_args not supported for protocol '%s'" % protocol)

        d = {'op': 'exp_request', 'format': 'json', 'ds': ds,
             'notify': notify, 'method': method, 'protocol': protocol}

        if filenamefmt is not None:
            d['filenamefmt'] = filenamefmt

        if requestor is None:
            d['requestor'] = notify.split('@')[0]
        elif requestor is not False:
            d['requestor'] = requestor

        query = '?' + urlencode(d)
        req = self._json_request(self._loc.url_jsoc_fetch + query)
        return req.data

    def exp_status(self, requestid):
        """
        Query data export status.

        Parameters
        ----------
        requestid : string
            Request identifier returned by exp_request.

        Returns
        -------
        result : dict
        """
        query = '?' + urlencode({'op': 'exp_status', 'requestid': requestid})
        req = self._json_request(self._loc.url_jsoc_fetch + query)
        return req.data


class SeriesInfo(object):
    def __init__(self, d, name=None):
        self.json = d
        self.name = name
        self.retention = self.json.get('retention')
        self.unitsize = self.json.get('unitsize')
        self.archive = self.json.get('archive')
        self.tapegroup = self.json.get('tapegroup')
        self.note = self.json.get('note')
        self.primekeys = self.json.get('primekeys')
        self.dbindex = self.json.get('dbindex')
        self.keywords = self._parse_keywords(d['keywords'])
        self.links = self._parse_links(d['links'])
        self.segments = self._parse_segments(d['segments'])

    @staticmethod
    def _parse_keywords(d):
        keys = [
            'name', 'type', 'recscope', 'defval', 'units', 'note', 'linkinfo']
        res = []
        for di in d:
            resi = []
            for k in keys:
                resi.append(di.get(k))
            res.append(tuple(resi))
        if not res:
            res = None  # workaround for older pandas versions
        res = pd.DataFrame(res, columns=keys)
        res.index = res.pop('name')
        res['is_time'] = (res.type == 'time')
        res['is_integer'] = (res.type == 'short')
        res['is_integer'] |= (res.type == 'int')
        res['is_integer'] |= (res.type == 'longlong')
        res['is_real'] = (res.type == 'float')
        res['is_real'] |= (res.type == 'double')
        res['is_numeric'] = (res.is_integer | res.is_real)
        return res

    @staticmethod
    def _parse_links(d):
        keys = ['name', 'target', 'kind', 'note']
        res = []
        for di in d:
            resi = []
            for k in keys:
                resi.append(di.get(k))
            res.append(tuple(resi))
        if not res:
            res = None  # workaround for older pandas versions
        res = pd.DataFrame(res, columns=keys)
        res.index = res.pop('name')
        return res

    @staticmethod
    def _parse_segments(d):
        keys = ['name', 'type', 'units', 'protocol', 'dims', 'note']
        res = []
        for di in d:
            resi = []
            for k in keys:
                resi.append(di.get(k))
            res.append(tuple(resi))
        if not res:
            res = None  # workaround for older pandas versions
        res = pd.DataFrame(res, columns=keys)
        res.index = res.pop('name')
        return res

    def __repr__(self):
        if self.name is None:
            return '<SeriesInfo>'
        else:
            return '<SeriesInfo "%s">' % self.name


class ExportRequest(object):
    """
    Class for handling data export requests. Use Client.export() or
    Client.export_from_id() to create an ExportRequest instance.
    """
    _status_code_ok = 0
    _status_code_notfound = 6
    _status_codes_pending = [1, 2, _status_code_notfound]
    _status_codes_ok_or_pending = [_status_code_ok] + _status_codes_pending

    def __init__(self, d, client, verbose=False):
        self._client = client
        self._verbose = verbose
        self._requestid = None
        self._status = None
        self._download_urls_cache = None
        self._update_status(d)

    @classmethod
    def _create_from_id(cls, requestid, client, verbose=False):
        d = client._json.exp_status(requestid)
        return cls(d, client, verbose)

    def __repr__(self):
        idstr = str(None) if self._requestid is None else (
            '"%s"' % self._requestid)
        return '<ExportRequest id=%s, status=%d>' % (idstr, self._status)

    @staticmethod
    def _parse_data(d):
        keys = ['record', 'filename']
        res = None if d is None else [
            (di.get(keys[0]), di.get(keys[1])) for di in d]
        if not res:
            res = None  # workaround for older pandas versions
        res = pd.DataFrame(res, columns=keys)
        return res

    def _update_status(self, d=None):
        if d is None and self._requestid is not None:
            d = self._client._json.exp_status(self._requestid)
        self._d = d
        self._d_time = time.time()
        self._status = int(self._d.get('status', self._status))
        self._requestid = self._d.get('requestid', self._requestid)
        if self._requestid is None:
            # Apparently 'reqid' is used instead of 'requestid' for certain
            # protocols like 'mpg'
            self._requestid = self._d.get('reqid')
        if self._requestid == '':
            # Use None if the requestid is empty (url_quick + as-is)
            self._requestid = None

    def _raise_on_error(self, notfound_ok=True):
        if self._status in self._status_codes_ok_or_pending:
            if self._status != self._status_code_notfound or notfound_ok:
                return  # request has not failed (yet)
        msg = self._d.get('error')
        if msg is None:
            msg = 'DRMS export request failed.'
        msg += ' [status=%d]' % self._status
        raise DrmsExportError(msg)

    def _generate_download_urls(self):
        """Generate download URLs for the current request."""
        res = self.data.copy()
        data_dir = self.dir

        # Clear first record name for movies, as it is not a DRMS record.
        if self.protocol in ['mpg', 'mp4']:
            if res.record[0].startswith('movie'):
                res.record[0] = None

        # tar exports provide only a single TAR file with full path
        if self.tarfile is not None:
            data_dir = None
            res = pd.DataFrame(
                [(None, self.tarfile)], columns=['record', 'filename'])

        # If data_dir is None, the filename column should contain the full
        # path of the file and we need to extract the basename part. If
        # data_dir contains a directory, the filename column should contain
        # only the basename and we need to join it with the directory.
        if data_dir is None:
            res.rename(columns={'filename': 'fpath'}, inplace=True)
            split_fpath = res.fpath.str.split('/')
            res['filename'] = [sfp[-1] for sfp in split_fpath]
        else:
            res['fpath'] = data_dir + '/' + res.filename

        if self.method.startswith('url'):
            baseurl = self._client.location.http_download_baseurl
        elif self.method.startswith('ftp'):
            baseurl = self._client.location.ftp_download_baseurl
        else:
            raise RuntimeError(
                'Download is not supported for export method "%s"' %
                self.method)

        # Generate download URLs.
        urls = []
        for fp in res.fpath:
            while fp.startswith('/'):
                fp = fp[1:]
            urls.append(urljoin(baseurl, fp))
        res['url'] = urls

        # Remove rows with missing files.
        res = res[res.filename != 'NoDataFile']

        del res['fpath']
        return res

    @staticmethod
    def _next_available_filename(fname):
        """Find next available filename, append a number if neccessary."""
        i = 1
        new_fname = fname
        while os.path.exists(new_fname):
            new_fname = '%s.%d' % (fname, i)
            i += 1
        return new_fname

    @property
    def verbose(self):
        return self._verbose

    @verbose.setter
    def verbose(self, value):
        self._verbose = bool(value)

    @property
    def json(self):
        """Dictionary containing the latest export request JSON reply"""
        return self._d

    @property
    def id(self):
        """Request ID"""
        return self._requestid

    @property
    def status(self):
        """Export request status"""
        return self._status

    @property
    def method(self):
        """Export method"""
        return self._d.get('method')

    @property
    def protocol(self):
        """Export protocol"""
        return self._d.get('protocol')

    @property
    def dir(self):
        """Common directory of the requested files on the server"""
        if self.has_finished(skip_update=True):
            self._raise_on_error()
        else:
            self.wait()
        data_dir = self._d.get('dir')
        return data_dir if data_dir else None

    @property
    def data(self):
        """
        Records and filenames of the export request.

        Returns a DataFrame containing the records and filenames of the
        export request (DataFrame columns: 'record', 'filename').
        """
        if self.has_finished(skip_update=True):
            self._raise_on_error()
        else:
            self.wait()
        return self._parse_data(self._d.get('data'))

    @property
    def tarfile(self):
        """Filename, if a TAR file was requested"""
        if self.has_finished(skip_update=True):
            self._raise_on_error()
        else:
            self.wait()
        data_tarfile = self._d.get('tarfile')
        return data_tarfile if data_tarfile else None

    @property
    def keywords(self):
        """Filename of textfile containing record keywords"""
        if self.has_finished(skip_update=True):
            self._raise_on_error()
        else:
            self.wait()
        data_keywords = self._d.get('keywords')
        return data_keywords if data_keywords else None

    @property
    def request_url(self):
        """URL of the export request"""
        data_dir = self.dir
        http_baseurl = self._client.location.http_download_baseurl
        if data_dir is None or http_baseurl is None:
            return None
        if data_dir.startswith('/'):
            data_dir = data_dir[1:]
        return urljoin(http_baseurl, data_dir)

    @property
    def urls(self):
        """
        URLs for all downloadable files of the export request.

        Returns a DataFrame containing the records, filenames and URLs of the
        export request (DataFrame columns: 'record', 'filename' and 'url').
        """
        if self._download_urls_cache is None:
            self._download_urls_cache = self._generate_download_urls()
        return self._download_urls_cache

    def has_finished(self, skip_update=False):
        """
        Check if the export request has finished.

        Parameters
        ----------
        skip_update : bool
            If set to True, the export status will not be updated from the
            server, even if it was in pending state after the last status
            update.

        Returns
        -------
        True if the export request has finished or False if the request is
        still pending.
        """
        pending = self._status in self._status_codes_pending
        if not pending:
            return True
        if not skip_update:
            self._update_status()
            pending = self._status in self._status_codes_pending
        return not pending

    def has_succeeded(self, skip_update=False):
        """
        Check if the export request has finished successfully.

        Parameters
        ----------
        skip_update : bool
            If set to True, the export status will not be updated from the
            server, even if it was in pending state after the last status
            update.

        Returns
        -------
        True if the export request has finished successfully or False if the
        request failed or is still pending.
        """
        if not self.has_finished(skip_update):
            return False
        return self._status == self._status_code_ok

    def has_failed(self, skip_update=False):
        """
        Check if the export request has finished unsuccessfully.

        Parameters
        ----------
        skip_update : bool
            If set to True, the export status will not be updated from the
            server, even if it was in pending state after the last status
            update.

        Returns
        -------
        True if the export request has finished unsuccessfully or False if the
        request has succeeded or is still pending.
        """
        if not self.has_finished(skip_update):
            return False
        return self._status not in self._status_codes_ok_or_pending

    def wait(self, timeout=None, sleep=5, retries_notfound=5, verbose=None):
        """
        Wait for the server to process the export request. This method
        continously updates the request status until the server signals
        that export request has succeeded or failed.

        Parameters
        ----------
        timeout : number or None
            Maximum number of seconds until this method times out. If set to
            None (the default), the status will be updated indefinitely until
            the request succeeded or failed.
        sleep : number or None
            Time in seconds between status updates (defaults to 5 seconds).
            If set to None, a server supplied value is used.
        retries_notfound : integer
            Number of retries in case the request was not found on the server.
            Note that it usually takes a short time until a new request is
            registered on the server, so a value too low might cause an
            exception to be raised, even if the request is valid and will
            eventually show up on the server.
        verbose : bool or None
            Set to True if status messages should be printed to stdout. If set
            to None, the verbose flag of the current ExportRequest instance is
            used instead.

        Returns
        -------
        True if the request succeeded or False if a timeout occured. In case
        of an error a DrmsExportError exception is raised.
        """
        if timeout is not None:
            t_start = time.time()
            timeout = float(timeout)
        if sleep is not None:
            sleep = float(sleep)
        retries_notfound = int(retries_notfound)
        if verbose is None:
            verbose = self._verbose

        # We are done, if the request has already finished.
        if self.has_finished(skip_update=True):
            self._raise_on_error()
            return True

        while True:
            if verbose:
                idstr = str(None) if self._requestid is None else (
                    '"%s"' % self._requestid)
                print('Export request pending. [id=%s, status=%d]' % (
                    idstr, self._status))

            # Use the user-provided sleep value or the server's wait value.
            # In case neither is available, wait for 5 seconds.
            wait_secs = self._d.get('wait', 5) if sleep is None else sleep

            # Consider the time that passed since the last status update.
            wait_secs -= (time.time() - self._d_time)
            if wait_secs < 0:
                wait_secs = 0

            if timeout is not None:
                # Return, if we would time out while sleeping.
                if t_start + timeout + wait_secs - time.time() < 0:
                    return False

            if verbose:
                print('Waiting for %d seconds...' % round(wait_secs))
            time.sleep(wait_secs)

            if self.has_finished():
                self._raise_on_error()
                return True
            elif self._status == self._status_code_notfound:
                # Raise exception, if no retries are left.
                if retries_notfound <= 0:
                    self._raise_on_error(notfound_ok=False)
                if verbose:
                    print('Request not found on server, %d retries left.' %
                          retries_notfound)
                retries_notfound -= 1

    def download(self, directory, index=None, fname_from_rec=None,
                 verbose=None):
        """
        Download data files. By default, filenames are generated from the
        corresponding record names (see parameter fname_from_rec). In case a
        file with the same name already exists in the download directory, an
        ascending number is appended to the filename.

        Note: Downloading data segments that are directories, e.g. data
        segments from series like "hmi.rdVflows_fd15_frame", is currently
        not supported. In order to download data from series like this,
        you need to use the export methods 'url-tar' or 'ftp-tar' when
        submitting the data export request.

        Parameters
        ----------
        directory : string
            Download directory (must already exist).
        index : integer, list of integers or None
            Index (or indices) of the file(s) to be downloaded. If set to
            None (the default), all files of the export request are
            downloaded. Note that this parameter is ignored for export methods
            'url-tar' and 'ftp-tar', where only a single tar file is available
            for download.
        fname_from_rec : bool or None
            If True, local filenames are generated from record names. If set to
            False, the original filenames are used. If set to None (the
            default), local filenames are generated only for export method
            'url_quick'. Exceptions: For exports with methods 'url-tar' and
            'ftp-tar', no filename will be generated. This also applies to
            movie files from exports with protocols 'mpg' or 'mp4', where the
            original filename is used locally.
        verbose : bool or None
            Set to True if status messages should be printed to stdout. If set
            to None, the verbose flag of the current ExportRequest instance is
            used instead.

        Returns
        -------
        result : pandas.DataFrame
            DataFrame containing the record string, download URL and local
            location of each downloaded file (DataFrame columns: 'record',
            'url' and 'download').
        """
        out_dir = os.path.abspath(directory)
        if not os.path.isdir(out_dir):
            raise IOError('Download directory "%s" does not exist' % out_dir)

        if np.isscalar(index):
            index = [int(index)]
        elif index is not None:
            index = list(index)

        if verbose is None:
            verbose = self.verbose

        # Wait until the export request has finished.
        self.wait(verbose=verbose)

        if fname_from_rec is None:
            # For 'url_quick', generate local filenames from record strings.
            if self.method == 'url_quick':
                fname_from_rec = True

        # self.urls contains the same records as self.data, except for the tar
        # methods, where self.urls only contains one entry, the TAR file.
        data = self.urls
        if index is not None and self.tarfile is None:
            data = data.iloc[index].copy()
        ndata = len(data)

        downloads = []
        for i in range(ndata):
            di = data.iloc[i]
            if fname_from_rec:
                filename = self._client._filename_from_export_record(
                    di.record, old_fname=di.filename)
                if filename is None:
                    filename = di.filename
            else:
                filename = di.filename

            fpath = os.path.join(out_dir, filename)
            fpath_new = self._next_available_filename(fpath)
            fpath_tmp = self._next_available_filename(fpath_new + '.part')
            if verbose:
                print('Downloading file %d of %d...' % (i + 1, ndata))
                print('    record: %s' % di.record)
                print('  filename: %s' % di.filename)
            try:
                urlretrieve(di.url, fpath_tmp)
            except (HTTPError, URLError):
                fpath_new = None
                if verbose:
                    print('  -> Error: Could not download file')
            else:
                fpath_new = self._next_available_filename(fpath)
                os.rename(fpath_tmp, fpath_new)
                if verbose:
                    print('  -> "%s"' % os.path.relpath(fpath_new))
            downloads.append(fpath_new)

        res = data[['record', 'url']].copy()
        res['download'] = downloads
        return res


class Client(object):
    def __init__(self, location='jsoc', encoding='latin1', debug=False):
        """
        Parameters
        ----------
        location : string or Location
            Registered server location ID or Location instance.
            Defaults to JSOC.
        encoding : string
            Character encoding (default is 'latin1').
        debug : boolean
            Enable or disable debug mode (default is disabled).
        """
        self._json = JsonClient(
            location=location, encoding=encoding, debug=debug)
        self._info_cache = {}

    def __repr__(self):
        return '<Client "%s">' % self.location.cgi_baseurl

    def _convert_numeric_keywords(self, ds, kdf, skip_conversion=None):
        si = self.info(ds)
        int_keys = list(si.keywords[si.keywords.is_integer].index)
        num_keys = list(si.keywords[si.keywords.is_numeric].index)
        num_keys += ['*recnum*', '*sunum*', '*size*']
        if skip_conversion is None:
            skip_conversion = []
        elif isinstance(skip_conversion, six.string_types):
            skip_conversion = [skip_conversion]
        for k in kdf:
            if k in skip_conversion:
                continue
            # pandas apparently does not support hexadecimal strings, so
            # we need a special treatment for integer strings that start
            # with '0x', like QUALITY. The following to_numeric call is
            # still neccessary as the results are still Python objects.
            if k in int_keys and kdf[k].dtype is np.dtype(object):
                idx = kdf[k].str.startswith('0x')
                if idx.any():
                    kdf.loc[idx, k] = kdf.loc[idx, k].map(
                        lambda x: int(x, base=16))
            if k in num_keys:
                kdf[k] = _pd_to_numeric_coerce(kdf.pop(k))

    @staticmethod
    def _raise_query_error(d, status=None):
        """Raises a DrmsQueryError, using the json error message from d"""
        if status is None:
            status = d.get('status')
        msg = d.get('error')
        if msg is None:
            msg = 'DRMS Query failed.'
        msg += ' [status=%s]' % status
        raise DrmsQueryError(msg)

    def _generate_filenamefmt(self, sname):
        """Generate filename format string for export requests."""
        try:
            si = self.info(sname)
        except:
            # Cannot generate filename format for unknown series.
            return None

        pkfmt_list = []
        for k in si.primekeys:
            if si.keywords.loc[k].is_time:
                pkfmt_list.append('{%s:A}' % k)
            else:
                pkfmt_list.append('{%s}' % k)

        if pkfmt_list:
            return '%s.%s.{segment}' % (si.name, '.'.join(pkfmt_list))
        else:
            return si.name + '.{recnum:%lld}.{segment}'

    # Some regular expressions used to parse export request queries.
    _re_export_recset = re.compile(
        r'^\s*([\w\.]+)\s*(\[.*\])?\s*(?:\{([\w\s\.,]*)\})?\s*$')
    _re_export_recset_pkeys = re.compile(r'\[([^\[^\]]*)\]')
    _re_export_recset_slist = re.compile(r'[\s,]+')

    @staticmethod
    def _parse_export_recset(rs):
        """Parse export request record set."""
        if rs is None:
            return None, None, None
        m = Client._re_export_recset.match(rs)
        if not m:
            return None, None, None
        sname, pkeys, segs = m.groups()
        if pkeys is not None:
            pkeys = Client._re_export_recset_pkeys.findall(pkeys)
        if segs is not None:
            segs = Client._re_export_recset_slist.split(segs)
        return sname, pkeys, segs

    def _filename_from_export_record(self, rs, old_fname=None):
        """Generate a filename from an export request record."""
        sname, pkeys, segs = self._parse_export_recset(rs)
        if sname is None:
            return None

        # We need to identify time primekeys and change the time strings to
        # make them suitable for filenames.
        try:
            si = self.info(sname)
        except:
            # Cannot generate filename for unknown series.
            return None

        if pkeys is not None:
            n = len(pkeys)
            if n != len(si.primekeys):
                # Number of parsed pkeys differs from series definition.
                return None
            for i in range(n):
                # Cleanup time strings.
                if si.keywords.loc[si.primekeys[i]].is_time:
                    v = pkeys[i]
                    v = v.replace('.', '').replace(':', '').replace('-', '')
                    pkeys[i] = v

        # Generate filename.
        fname = si.name
        if pkeys is not None:
            pkeys = [k for k in pkeys if k.strip()]
            pkeys_str = '.'.join(pkeys)
            if pkeys_str:
                fname += '.' + pkeys_str
        if segs is not None:
            segs = [s for s in segs if s.strip()]
            segs_str = '.'.join(segs)
            if segs_str:
                fname += '.' + segs_str

        if old_fname is not None:
            # Try to use the file extension of the original filename.
            known_fname_extensions = [
                '.fits', '.txt', '.jpg', '.mpg', '.mp4', '.tar']
            for ext in known_fname_extensions:
                if old_fname.endswith(ext):
                    return fname + ext
        return fname

    @property
    def location(self):
        return self._json.location

    @property
    def debug(self):
        return self._json.debug

    @debug.setter
    def debug(self, value):
        self._json.debug = value

    def series(self, ds_filter=None):
        """
        List available data series.

        Parameters
        ----------
        ds_filter : string or None
            Regular expression to select a subset of the available series.
            If set to None, a list of all available series is returned.

        Returns
        -------
        result : pandas.DataFrame
            DataFrame containing names, primekeys and notes of the selected
            series.
        """
        d = self._json.show_series(ds_filter)
        status = d.get('status')
        if status != 0:
            self._raise_query_error(d)
        keys = ('name', 'primekeys', 'note')
        if not d['names']:
            return pd.DataFrame(columns=keys)
        recs = [(it['name'], _split_arg(it['primekeys']), it['note'])
                for it in d['names']]
        return pd.DataFrame(recs, columns=keys)

    def info(self, ds):
        """
        Get information about the content of a data series.

        Parameters
        ----------
        ds : string
            Name of the data series.

        Returns
        -------
        result : SeriesInfo
            SeriesInfo instance containing information about the data series.
        """
        name = _extract_series_name(ds)
        if name is not None:
            name = name.lower()
        if name in self._info_cache:
            return self._info_cache[name]
        d = self._json.series_struct(name)
        status = d.get('status')
        if status != 0:
            self._raise_query_error(d)
        si = SeriesInfo(d, name=name)
        if name is not None:
            self._info_cache[name] = si
        return si

    def keys(self, ds):
        """
        Get a list of keywords that are available for a series. Use the info()
        method for more details.

        Parameters
        ----------
        ds : string
            Name of the data series.

        Returns
        -------
        result : list
            List of keywords available for the selected series.
        """
        si = self.info(ds)
        return list(si.keywords.index)

    def pkeys(self, ds):
        """
        Get a list of primekeys that are available for a series. Use the info()
        method for more details.

        Parameters
        ----------
        ds : string
            Name of the data series.

        Returns
        -------
        result : list
            List of primekeys available for the selected series.
        """
        si = self.info(ds)
        return list(si.primekeys)

    def get(self, ds, key=None, seg=None, link=None, convert_numeric=True,
            skip_conversion=None):
        """
        This method is deprecated. Use Client.query() instead.
        """
        warnings.warn(
            'Client.get() is deprecated, use Client.query() instead',
            DeprecationWarning)
        return self.query(
            ds, key=key, seg=seg, link=link, convert_numeric=convert_numeric,
            skip_conversion=skip_conversion)

    def query(self, ds, key=None, seg=None, link=None, convert_numeric=True,
              skip_conversion=None, pkeys=False):
        """
        Query keywords, segments and/or links of a record set. At least one
        of out of the key, seg or link parameters must be specified.

        Parameters
        ----------
        ds : string
            Record set query.
        key : string, list of strings or None
            List of requested keywords, optional. If set to None (default),
            no keyword results will be returned, except when pkeys is True.
        seg : string, list of strings or None
            List of requested segments, optional. If set to None (default),
            no segment results will be returned.
        link : string, list of strings or None
            List of requested Links, optional. If set to None (default), no
            link results will be returned.
        convert_numeric : boolean
            Convert keywords with numeric types from string to numbers. This
            may result in NaNs for invalid/missing values. Default is True.
        skip_conversion : list of strings or None
            List of keywords names to be skipped when performing a numeric
            conversion. Default is None.
        pkeys : boolean
            If True, all primekeys of the series are added to the key list.

        Returns
        -------
        res_key : pandas.DataFrame (if key is not None or pkeys is True)
            Queried keywords
        res_seg : pandas.DataFrame (if seg is not None)
            Queried segments
        res_link : pandas.DataFrame (if link is not None)
            Queried links
        """
        if pkeys:
            pk = self.pkeys(ds)
            key = _split_arg(key) if key is not None else []
            key = [k for k in key if k not in pk]
            key = pk + key

        lres = self._json.rs_list(ds, key, seg, link)
        status = lres.get('status')
        if status != 0:
            self._raise_query_error(lres)

        res = []
        if key is not None:
            if 'keywords' in lres:
                names = [it['name'] for it in lres['keywords']]
                values = [it['values'] for it in lres['keywords']]
                res_key = pd.DataFrame.from_items(zip(names, values))
            else:
                res_key = pd.DataFrame()
            if convert_numeric:
                self._convert_numeric_keywords(ds, res_key, skip_conversion)
            res.append(res_key)

        if seg is not None:
            if 'segments' in lres:
                names = [it['name'] for it in lres['segments']]
                values = [it['values'] for it in lres['segments']]
                res_seg = pd.DataFrame.from_items(zip(names, values))
            else:
                res_seg = pd.DataFrame()
            res.append(res_seg)

        if link is not None:
            if 'links' in lres:
                names = [it['name'] for it in lres['links']]
                values = [it['values'] for it in lres['links']]
                res_link = pd.DataFrame.from_items(zip(names, values))
            else:
                res_link = pd.DataFrame()
            res.append(res_link)

        if len(res) == 0:
            return None
        elif len(res) == 1:
            return res[0]
        else:
            return tuple(res)

    def export_old(self, ds, requestor, notify):
        """
        This function exports data as FITS files. To do this, the function
        binds metadata (keywords) to images (arrays) to create FITS files and
        then serves the FITS files at jsoc.stanford.edu.
        Written by Monica Bobra and Art Amezcua
        19 July 2016

        Parameters
        ----------
        requestor : string
            Username of requestor.
        notify : string
            E-mail address of requestor.
        ds : string
            Name of the data series.

        Returns
        -------
        supath : list
            List containing paths to all the requested FITS files.
        """
        return self._json.export_old(ds, requestor, notify)

    def check_email(self, email):
        """
        Check if the email address is registered for data export. You can
        register your email for data export from JSOC at:

            http://jsoc.stanford.edu/ajax/register_email.html

        Parameters
        ----------
        email : string
            Email address to be checked.

        Returns
        -------
        True if the email address is valid and registered, False otherwise.
        """
        res = self._json.check_address(email)
        status = res.get('status')
        return status is not None and int(status) == 2

    def export(self, ds, email, method='url_quick', protocol='as-is',
               protocol_args=None, filenamefmt=None, requestor=None,
               verbose=False):
        """
        Submit a data export request.

        Parameters
        ----------
        ds : string
        email : string
        method : string
        protocol : string
        protocol_args : dict
        filenamefmt : string, None or False
        requestor : string, None or False
        verbose : bool

        Returns
        -------
        result : ExportRequest
        """
        if filenamefmt is None:
            sname = _extract_series_name(ds)
            filenamefmt = self._generate_filenamefmt(sname)
        elif filenamefmt is False:
            filenamefmt = None
        d = self._json.exp_request(
            ds, email, method=method, protocol=protocol,
            protocol_args=protocol_args, filenamefmt=filenamefmt,
            requestor=requestor)
        return ExportRequest(d, client=self, verbose=verbose)

    def export_from_id(self, requestid, verbose=False):
        """
        Create an ExportRequest instance from an already existing requestid.

        Parameters
        ----------
        requestid : string

        Returns
        -------
        result : ExportRequest
        """
        return ExportRequest._create_from_id(
            requestid, client=self, verbose=verbose)


if __name__ == '__main__':
    def _test_info(c, ds):
        sname = c.series(ds).name
        res = []
        skiplist = [r'jsoc.*']
        for sni in sname:
            skipit = False
            print(sni)
            for spat in skiplist:
                if re.match(spat, sni):
                    print('** skipping series **')
                    skipit = True
                    break
            if not skipit:
                res.append(c.info(sni))
        return res

    c = Client(debug=True)
