"""Feishu HTTP and canonical Bitable adapter. Tokens are supplied by the host environment."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import json
import os
import uuid

from .schema import TABLES, ENUMS


class FeishuError(ValueError):
    def __init__(self, message, *, code=None, uncertain=False):
        super().__init__(message)
        self.code, self.uncertain = code, uncertain


class WriteConflict(ValueError):
    """A fresh pre-write record no longer matches the reviewed snapshot."""


def _part(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Missing configured remote identifier')
    return quote(value, safe='')


class FeishuClient:
    def __init__(self, token=None, *, token_env='FEISHU_ACCESS_TOKEN', transport=None):
        self.token = token or os.environ.get(token_env)
        self.transport = transport
        if not self.token and not transport:
            raise ValueError(f'Missing access token environment variable: {token_env}')

    def request(self, method, path, *, body=None, query=None, raw=False):
        if not path.startswith('/') or '://' in path:
            raise ValueError('Only Feishu API paths are accepted')
        url = 'https://open.feishu.cn/open-apis' + path
        if query:
            url += '?' + urlencode({k: v for k, v in query.items() if v is not None})
        try:
            if self.transport:
                result = self.transport(method, path, body, query or {}, raw)
            else:
                payload = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
                req = Request(url, data=payload, method=method, headers={
                    'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json; charset=utf-8'})
                with urlopen(req, timeout=30) as response:
                    content = response.read()
                result = content if raw else json.loads(content)
        except HTTPError as exc:
            # Do not log request headers/tokens or arbitrary response bodies.
            raise FeishuError(f'Feishu HTTP {exc.code}', code=exc.code,
                              uncertain=method != 'GET' and exc.code >= 500) from None
        except (URLError, TimeoutError, ConnectionError, OSError, json.JSONDecodeError) as exc:
            raise FeishuError('Feishu response unavailable; reconcile writes before retry',
                              uncertain=method != 'GET') from None
        if raw:
            # Export failures may still be JSON instead of transcript bytes.
            if isinstance(result, bytes) and result.lstrip().startswith(b'{'):
                try:
                    error = json.loads(result)
                    if error.get('code'):
                        raise FeishuError('Minutes export rejected', code=error['code'])
                except json.JSONDecodeError:
                    pass
            return result.decode('utf-8-sig') if isinstance(result, bytes) else result
        if not isinstance(result, dict) or 'code' not in result:
            raise FeishuError('Malformed Feishu response', uncertain=method != 'GET')
        if result['code'] != 0:
            raise FeishuError(f'Feishu API error {result["code"]}', code=result['code'])
        return result.get('data', {})

    def pages(self, method, path, *, body=None, query=None):
        query = dict(query or {})
        query.setdefault('page_size', 500)
        seen = set()
        items = []
        while True:
            page = self.request(method, path, body=body, query=query)
            if not isinstance(page.get('items', []), list):
                raise FeishuError('Incomplete record page')
            items.extend(page.get('items', []))
            if not page.get('has_more'):
                return items
            token = page.get('page_token')
            if not token or token in seen:
                raise FeishuError('Broken/repeated pagination token; snapshot is incomplete')
            seen.add(token)
            query['page_token'] = token

    def minutes(self, token):
        path = '/minutes/v1/minutes/' + _part(token)
        metadata = self.request('GET', path)
        artifacts = self.request('GET', path + '/artifacts')
        transcript = self.request('GET', path + '/transcript', raw=True,
                                  query={'need_speaker': 'true', 'need_timestamp': 'true', 'file_format': 'txt'})
        import hashlib
        return {'minute_token': token, 'metadata': metadata, 'artifacts': artifacts,
                'transcript': transcript, 'sha256': hashlib.sha256(transcript.encode()).hexdigest(),
                'read_at': datetime.now(timezone.utc).isoformat(), 'kind': 'ai_and_transcript',
                'decision_confirmed': False}

    def mention(self, chat_id, user_id, text, *, request_id):
        # Structured post at element: display names cannot create real mentions.
        return self.request('POST', '/im/v1/messages', query={'receive_id_type': 'chat_id'}, body={
            'receive_id': chat_id, 'msg_type': 'post', 'uuid': request_id,
            'content': json.dumps({'zh_cn': {'title': '项目待维护事项', 'content': [[
                {'tag': 'at', 'user_id': user_id}, {'tag': 'text', 'text': text}]]}}, ensure_ascii=False)})

    def event(self, calendar_id, values, *, event_id=None):
        path = '/calendar/v4/calendars/' + _part(calendar_id) + '/events'
        if event_id:
            path += '/' + _part(event_id)
        return self.request('PATCH' if event_id else 'POST', path, body=values)

    def read_event(self, calendar_id, event_id):
        return self.request('GET', '/calendar/v4/calendars/' + _part(calendar_id) + '/events/' + _part(event_id))

    def invite(self, calendar_id, event_id, attendees):
        return self.request('POST', '/calendar/v4/calendars/' + _part(calendar_id) + '/events/' +
                            _part(event_id) + '/attendees', query={'user_id_type': 'open_id'},
                            body={'attendees': [{'type': 'user', 'user_id': x} for x in attendees],
                                  'need_notification': True})

    def document(self, title, paragraphs, *, folder_token=None):
        created = self.request('POST', '/docx/v1/documents', body={
            'title': title, **({'folder_token': folder_token} if folder_token else {})})
        document_id = created['document']['document_id']
        # Each created document is a new immutable report version, never overwrite prior material.
        try:
            for offset in range(0, len(paragraphs), 50):
                children = [{'block_type': 2, 'text': {'elements': [
                    {'text_run': {'content': str(p)}}]}} for p in paragraphs[offset:offset + 50]]
                self.request('POST', '/docx/v1/documents/' + _part(document_id) + '/blocks/' +
                             _part(document_id) + '/children', body={'children': children, 'index': -1})
        except Exception:
            raise FeishuError(f'Document {document_id} created; content incomplete, inspect before retry',
                              uncertain=True) from None
        return {'document_id': document_id, 'url': created['document'].get('url')}


class BitableAdapter:
    """Explicit field/enum/reference mapping and opt-in stable remote identity."""
    def __init__(self, client, config):
        self.client, self.config = client, config
        self.base = _part(config['base_token'])
        self.tables = config['tables']
        self.timezone = ZoneInfo(config['timezone'])
        for table, spec in self.tables.items():
            if table not in TABLES:
                raise ValueError('Unknown table: ' + table)
            identity = spec.get('identity', 'field')
            if identity not in ('field', 'record_id'):
                raise ValueError('Unknown identity mode: ' + table)
            if identity == 'field' and 'id' not in spec.get('fields', {}):
                raise ValueError('Unknown table or missing business ID mapping: ' + table)
            if identity == 'record_id' and 'id' in spec.get('fields', {}):
                raise ValueError('record_id identity cannot map a display field as id: ' + table)
            known = {f.name: f for f in TABLES[table]['fields']}
            if set(spec['fields']) - set(known):
                raise ValueError('Unknown canonical fields: ' + table)
            absent = spec.get('confirmed_absent_fields', [])
            if not isinstance(absent, list) or any(not isinstance(f, str) or f not in known or
                    known[f].required or f in ('id', 'version') for f in absent):
                raise ValueError('Only optional known fields may be confirmed absent: ' + table)
            if set(absent) & set(spec['fields']):
                raise ValueError('Field cannot be both mapped and confirmed absent: ' + table)
            for field in spec['fields']:
                mapped = self._spec(table, field)
                if mapped.get('type') == 'formula' and (mapped.get('writable', False) or field == 'id'):
                    raise ValueError('Formula is read-only and cannot be a stable id: ' + table + '.' + field)
            display = spec.get('display_field')
            if display and (identity != 'record_id' or not isinstance(display, dict) or
                            display.get('type') not in ('text', 'formula') or display.get('writable', False)):
                raise ValueError('Display field requires record_id identity and read-only text/formula mapping')
            _part(spec['table_id'])
        self._maps = {}
        self.display_values = {}

    def assigns_record_id(self, table):
        return self.tables[table].get('identity', 'field') == 'record_id'

    def _record_key(self, table, record):
        if self.assigns_record_id(table):
            return record.get('record_id')
        return self._value(table, 'id', record['fields'].get(self._spec(table, 'id')['name']))

    def _path(self, table):
        return '/bitable/v1/apps/' + self.base + '/tables/' + _part(self.tables[table]['table_id'])

    def _spec(self, table, field):
        spec = self.tables[table]['fields'][field]
        return {'name': spec, 'type': 'text'} if isinstance(spec, str) else spec

    def _value(self, table, field, value, *, encode=False, spec=None):
        spec = spec or self._spec(table, field)
        typ = spec.get('type', 'text')
        if typ == 'formula':
            if encode:
                raise ValueError('Formula is read-only: ' + field)
            # Only textual formula results are mapped in this adapter. Other
            # result kinds need an explicit typed contract, never str(dict).
            if isinstance(value, dict):
                if value.get('type') != 1:
                    raise ValueError('Expected textual formula result: ' + field)
                value = value.get('value')
            if value is not None and not isinstance(value, (str, list)):
                raise ValueError('Expected textual formula result: ' + field)
            if isinstance(value, list) and any(not isinstance(v, dict) or
                    not isinstance(v.get('text'), str) for v in value):
                raise ValueError('Expected textual formula segments: ' + field)
            typ = 'text'
        if value is None:
            return None
        if typ in ('date', 'datetime'):
            if encode:
                dt = datetime.fromisoformat(value)
                if dt.tzinfo is None:
                    if typ == 'datetime':
                        raise ValueError('Datetime must include timezone')
                    dt = dt.replace(tzinfo=self.timezone)
                return int(dt.timestamp() * 1000)
            dt = datetime.fromtimestamp(value / 1000, self.timezone)
            return dt.date().isoformat() if typ == 'date' else dt.isoformat()
        if typ in ('relation', 'person'):
            target = spec.get('table', 'people')
            mapping = self._user_maps if typ == 'person' else self._maps.get(target)
            if mapping is None:
                raise ValueError('Related table is not in complete snapshot: ' + target)
            many = spec.get('many', False)
            if typ == 'relation' and not encode and isinstance(value, dict) and 'link_record_ids' in value:
                value = value['link_record_ids']
                if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
                    raise ValueError('Invalid linked record IDs: ' + table + '.' + field)
            vals = value if isinstance(value, list) else [value]
            if not encode:
                vals = [v.get('id' if typ == 'person' else 'record_id') if isinstance(v, dict) else v for v in vals]
                mapping = {v: k for k, v in mapping.items()}
            if any(v not in mapping for v in vals):
                raise ValueError('Unresolved relation ' + table + '.' + field)
            result = [mapping[v] for v in vals]
            if encode and typ == 'person':
                return [{'id': v} for v in result]
            if encode or many:
                return result
            if len(result) > 1:
                raise ValueError('Multiple records in a single reference')
            return result[0] if result else None
        if typ == 'select':
            options = spec.get('options', {})
            mapping = options if encode else {v: k for k, v in options.items()}
            if value not in mapping:
                raise ValueError('Unmapped selection: ' + table + '.' + field)
            return mapping[value]
        if typ == 'link':
            return {'text': value, 'link': value} if encode else value.get('link')
        if typ == 'text' and not encode and isinstance(value, list):
            return ''.join(v.get('text', '') for v in value)
        if typ == 'number' and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError('Invalid numeric field')
        if typ == 'checkbox' and not isinstance(value, bool):
            raise ValueError('Invalid boolean field')
        return value

    def read_all(self):
        missing = set(TABLES) - set(self.tables) - set(self.config.get('confirmed_empty_tables', []))
        if missing:
            raise ValueError('Unread table coverage; map or explicitly confirm empty: ' + ','.join(sorted(missing)))
        for table, spec in self.tables.items():
            required = {f.name for f in TABLES[table]['fields'] if f.name != 'version'}
            covered = set(spec['fields']) | set(spec.get('confirmed_absent_fields', []))
            if self.assigns_record_id(table):
                covered.add('id')
            if required - covered:
                raise ValueError('Missing required field coverage in ' + table + ': ' + ','.join(sorted(required - covered)))
        raw = {}
        self._maps = {}
        self.display_values = {}
        for table in self.tables:
            remote_fields = self.client.pages('GET', self._path(table) + '/fields', query={'page_size': 100})
            discovered = {f['field_name']: f['type'] for f in remote_fields}
            types = {'text': {1}, 'formula': {20}, 'number': {2}, 'select': {3}, 'date': {5}, 'datetime': {5}, 'checkbox': {7}, 'link': {15}, 'relation': {18, 21}, 'person': {11}}
            mappings = [(name, self._spec(table, name)) for name in self.tables[table]['fields']]
            if self.tables[table].get('display_field'):
                mappings.append(('display_field', self.tables[table]['display_field']))
            for name, spec in mappings:
                if discovered.get(spec['name']) not in types.get(spec.get('type', 'text'), set()):
                    raise ValueError('Remote field missing/type changed: ' + table + '.' + name)
            raw[table] = self.client.pages('POST', self._path(table) + '/records/search', body={},
                                           query={'user_id_type': 'open_id'})
            mapping = {}
            for r in raw[table]:
                value = self._record_key(table, r)
                if not isinstance(value, str) or not value or value in mapping:
                    raise ValueError('Missing/duplicate business ID in ' + table)
                if not isinstance(r.get('record_id'), str) or not r['record_id'] or r['record_id'] in mapping.values():
                    raise ValueError('Missing/duplicate remote record_id in ' + table)
                mapping[value] = r['record_id']
            self._maps[table] = mapping
        self._user_maps = {}
        if 'people' in raw and 'feishu_user_id' in self.tables['people']['fields']:
            for person in raw['people']:
                pid = self._record_key('people', person)
                uid = self._value('people', 'feishu_user_id', person['fields'].get(self._spec('people', 'feishu_user_id')['name']))
                if uid:
                    if uid in self._user_maps.values():
                        raise ValueError('Ambiguous Feishu person identity')
                    self._user_maps[pid] = uid
        result = {table: [] for table in TABLES}
        for table, records in raw.items():
            for r in records:
                row = {field: self._value(table, field, r['fields'].get(self._spec(table, field)['name']))
                       for field in self.tables[table]['fields']}
                row.update({field: None for field in self.tables[table].get('confirmed_absent_fields', [])})
                row['id'] = self._record_key(table, r)
                display = self.tables[table].get('display_field')
                if display:
                    self.display_values.setdefault(table, {})[row['id']] = self._value(
                        table, 'display_field', r['fields'].get(display['name']), spec=display)
                result[table].append(row)
        if self.config.get('project_id'):
            projects = result['projects']
            if len(projects) != 1 or projects[0]['id'] != self.config['project_id']:
                raise ValueError('Remote snapshot does not match the configured single project')
        return result

    def read(self, table, record_id):
        return next((r for r in self.read_all()[table] if r['id'] == record_id), None)

    @staticmethod
    def _check_wbs_write(operation, table, old, values):
        if table != 'wbs':
            return
        if old and old.get('migrated_operation_id'):
            raise ValueError('migrated_readonly: historical WBS alias is immutable; maintain its operations record')
        if operation != 'delete':
            scope = values.get('scope', (old or {}).get('scope', 'project'))
            if scope != 'project':
                raise ValueError('operations_separate: nonproject work belongs in the operations table')

    def validate_write(self, operation, table, record_id, values):
        from .store import _normalise, validate_state
        data = self.read_all()
        previous = {t: {r['id']: r for r in rows} for t, rows in data.items()}
        old = previous[table].get(record_id)
        self._check_wbs_write(operation, table, old, values)
        self._encode_fields(table, values)
        if operation == 'delete':
            data[table] = [r for r in data[table] if r['id'] != record_id]
        else:
            row = _normalise(table, dict(values, id=record_id), old)
            row['version'] = (old or {}).get('version') or 1
            data[table] = [r for r in data[table] if r['id'] != record_id] + [row]
        validate_state(data, previous=previous)

    def _encode_fields(self, table, values):
        encoded = {}
        for field, value in values.items():
            if field not in self.tables[table]['fields']:
                raise ValueError('Field not mapped: ' + field)
            spec = self._spec(table, field)
            if spec.get('writable', True) is False or spec.get('type') == 'formula' or field == 'version':
                raise ValueError('Field is read-only: ' + field)
            encoded[spec['name']] = self._value(table, field, value, encode=True)
        return encoded

    def write(self, operation, table, record_id, values, request_id, *, expected=None):
        if table not in self.tables:
            raise ValueError('Table outside configured Base')
        data = self.read_all()
        current = next((r for r in data[table] if r['id'] == record_id), None)
        if current != expected:
            raise WriteConflict('Record changed before remote write; reread and review a corrective action')
        self._check_wbs_write(operation, table, current, values)
        # Bitable provides no compare-and-swap for this endpoint. This fresh
        # check narrows the race; host field permissions/single-writer routing
        # are still needed for strict concurrent-write exclusion.
        if operation == 'delete':
            remote = self._maps[table][record_id]
            return self.client.request('DELETE', self._path(table) + '/records/' + _part(remote))
        encoded = self._encode_fields(table, values)
        path = self._path(table) + '/records'
        query = {'user_id_type': 'open_id'}
        if operation == 'create':
            if record_id in self._maps[table]:
                raise ValueError('Business ID already exists; reconcile instead of duplicate create')
            query['client_token'] = request_id
        else:
            path += '/' + _part(self._maps[table][record_id])
        return self.client.request('POST' if operation == 'create' else 'PUT', path,
                                   body={'fields': encoded}, query=query)

    def references(self, table, record_id):
        data = self.read_all()
        missing = set(TABLES) - set(self.tables)
        dependents = {t for t, meta in TABLES.items() if any(f.ref == table for f in meta['fields'])}
        if missing & dependents:
            raise ValueError('Deletion cannot verify unmapped referencing tables: ' + ','.join(sorted(missing & dependents)))
        refs = []
        for t, rows in data.items():
            for f in TABLES[t]['fields']:
                if f.ref != table:
                    continue
                refs.extend((t, r['id'], f.name) for r in rows if r.get(f.name) == record_id or
                            isinstance(r.get(f.name), list) and record_id in r[f.name])
        return refs
