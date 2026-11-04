"""Block known credentials and recognised document identifiers in public queries.

This is a bounded defence in addition to instruction hierarchy and narrow tools,
not a claim of a complete DLP classifier for every possible encoding or document.
"""
import json
import re
from urllib.parse import unquote

from django.conf import settings
from django.core.exceptions import PermissionDenied


def _normal(value):
    return re.sub(r'[^\w]','',str(value).casefold())


class EgressGuard:
    sensitive_keys=re.compile(r'booking_reference|reservation_code|passport|pnr|passengers?|ticket_number|card_number|api_key|secret|token',re.I)
    labels=re.compile(r'(?:паспорт|passport|pnr|код брони|booking reference|номер билета)\s*[:#№]?\s*([A-ZА-Я0-9][A-ZА-Я0-9 -]{4,28})',re.I)

    def __init__(self,input_text=''):
        self.values=set()
        self.phrases=set()
        for key in ('LLM_API_KEY','BRAVE_SEARCH_API_KEY','GOOGLE_MAPS_API_KEY','GOOGLE_CLIENT_SECRET','TELEGRAM_BOT_TOKEN','TOKEN_ENCRYPTION_KEY','DJANGO_SECRET_KEY'):
            value=getattr(settings,key,'')
            if value and len(str(value))>=8:self.values.add(_normal(value))
        self._text(input_text)

    def _add(self,value):
        if isinstance(value,str) and 4<=len(value)<=500 and len(self.values)<2000:
            self.values.add(_normal(value))
        elif isinstance(value,list):
            for entry in value:self._add(entry)
        elif isinstance(value,dict):
            for entry in value.values():self._add(entry)

    def _text(self,text):
        if not isinstance(text,str):return
        for match in self.labels.finditer(text):self._add(match.group(1).strip())

    def _document_phrases(self, text):
        if not isinstance(text, str) or len(self.phrases) >= 3000:
            return
        words = re.findall(r'\w+', text.casefold())
        for index in range(len(words) - 7):
            phrase = ''.join(words[index:index+8])
            if len(phrase) >= 40:
                self.phrases.add(phrase)
                if len(self.phrases) >= 3000:
                    return

    def observe(self,result):
        def walk(value):
            if isinstance(value,dict):
                for key,child in value.items():
                    # Protect long quotes from captions, retrieved chunks and
                    # search excerpts while permitting short names and addresses.
                    if key == 'telegram_caption' or (value.get('document_id') and key in ('text', 'excerpt')):
                        self._document_phrases(child)
                    if self.sensitive_keys.search(key):self._add(child)
                    else:walk(child)
            elif isinstance(value,list):
                for child in value:walk(child)
            elif isinstance(value,str):self._text(value)
        walk(result)

    def check(self,arguments):
        value=json.dumps(arguments,ensure_ascii=False)
        for _ in range(3):value=unquote(value)
        normalized=_normal(value)
        if any(secret and secret in normalized for secret in self.values) or any(phrase in normalized for phrase in self.phrases):
            raise PermissionDenied('Публичный запрос содержит известный секрет, реквизиты документа или длинную цитату из личного файла. Используйте обезличенный запрос; для маршрута — только адреса.')
