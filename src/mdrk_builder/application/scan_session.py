"""One local scan session shared by document projections; no disk cache or patient mixing."""
from copy import deepcopy
from hashlib import sha256
from threading import Event


class ScanCancelled(Exception):
    pass


class ScanSession:
    def __init__(self):
        self.cache = {}
        self.cancelled = Event()
        self.progress = lambda done,total,path: None
        self.folder = None

    def begin(self, folder):
        if self.folder != folder:
            self.cache.clear()
            self.folder = folder
        self.cancelled.clear()

    def check(self):
        if self.cancelled.is_set():
            raise ScanCancelled('Считывание отменено; предыдущие данные сохранены.')

    def cached(self, path):
        self.check()
        fingerprint=sha256(path.read_bytes()).hexdigest()
        entry=self.cache.get(path)
        return fingerprint, deepcopy(entry[1]) if entry and entry[0]==fingerprint else None

    def put(self,path,fingerprint,document):
        self.cache[path]=(fingerprint,deepcopy(document))

    def parsed(self,path):
        item=self.cache.get(path)
        return item[1].document if item else None
