from app.cache import Cache
from app.db import Base, make_database
from app.index import LocalIndex, OpenSearchIndex
from app.retrieval import Embeddings


class Runtime:
    def __init__(self, settings):
        self.settings = settings
        self.engine, self.session = make_database(settings.database_url)
        self.embeddings = Embeddings(settings)
        self.cache = Cache(settings)
        self.index = OpenSearchIndex(self) if settings.search_backend == "opensearch" else LocalIndex(self)

    def initialize(self):
        Base.metadata.create_all(self.engine)
        self.index.initialize()

    def close(self):
        self.index.close()
        self.embeddings.client.close()
        if self.cache.redis:
            self.cache.redis.close()
        self.engine.dispose()
