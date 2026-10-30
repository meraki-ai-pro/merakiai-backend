"""Deleting a lecture video removes it for real; a live link cannot be added twice."""

import pytest
from fastapi import HTTPException

from app.api.v1.lecturer import knowledge


class Table:
    def __init__(self, db, name):
        self.db, self.name, self.filters, self.op, self.payload = db, name, [], "select", None

    def select(self, *_):
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def execute(self):
        rows = [r for r in self.db.tables.setdefault(self.name, []) if all(r.get(c) == v for c, v in self.filters)]
        if self.op == "update":
            for r in rows:
                r.update(self.payload)
        elif self.op == "delete":
            self.db.tables[self.name] = [r for r in self.db.tables[self.name] if r not in rows]
        return type("R", (), {"data": rows})()


class Bucket:
    def __init__(self, db):
        self.db = db

    def remove(self, paths):
        self.db.removed.extend(paths)


class DB:
    def __init__(self, **tables):
        self.tables, self.removed = tables, []
        self.storage = type("S", (), {"from_": lambda _s, _b: Bucket(self)})()

    def table(self, name):
        return Table(self, name)


@pytest.fixture
def env(monkeypatch):
    purged = []
    monkeypatch.setattr(knowledge, "assert_course_owner", lambda *_: None)
    monkeypatch.setattr(knowledge, "invalidate_visibility", lambda *_: None)
    monkeypatch.setattr(knowledge.audit, "record", lambda **_: None)
    monkeypatch.setattr("app.ai.ingestion.pinecone.delete_vectors", lambda ids, ns: purged.append((ns, sorted(ids))))

    def install(db):
        monkeypatch.setattr(knowledge, "get_supabase", lambda: db)
        return db

    return install, purged


LINK = "https://www.youtube.com/watch?v=abc"


def test_deleting_a_youtube_link_clears_it_and_purges_its_vectors(env):
    install, purged = env
    db = install(DB(
        documents=[{"id": "d1", "course_id": "c1", "title": "Limits", "is_published": True, "media_url": LINK}],
        document_chunks=[{"document_id": "d1", "pinecone_id": "v1", "pinecone_namespace": "ns"},
                         {"document_id": "d1", "pinecone_id": "v2", "pinecone_namespace": "ns"}],
    ))
    knowledge.delete_knowledge("c1", "d1", None, {"id": "u"})
    doc = db.tables["documents"][0]
    assert doc["media_url"] is None and doc["is_published"] is False and doc["deleted_at"]
    assert purged == [("ns", ["v1", "v2"])]
    assert db.tables["document_chunks"] == []
    assert db.removed == []  # nothing in storage for a link


def test_deleting_an_uploaded_video_removes_the_file(env):
    install, _ = env
    db = install(DB(documents=[{"id": "d2", "course_id": "c1", "title": "Lecture", "is_published": True,
                                "source_filename": "week1.mp4", "storage_path": "c1/d2/week1.mp4"}],
                    document_chunks=[]))
    knowledge.delete_knowledge("c1", "d2", None, {"id": "u"})
    assert db.removed == ["c1/d2/week1.mp4"]


def test_deleting_a_pdf_stays_a_soft_delete(env):
    install, purged = env
    db = install(DB(documents=[{"id": "d3", "course_id": "c1", "title": "Notes", "is_published": True,
                                "source_filename": "notes.pdf", "storage_path": "c1/d3/notes.pdf"}],
                    document_chunks=[{"document_id": "d3", "pinecone_id": "v9", "pinecone_namespace": "ns"}]))
    knowledge.delete_knowledge("c1", "d3", None, {"id": "u"})
    assert purged == [] and len(db.tables["document_chunks"]) == 1 and db.removed == []


def test_a_live_link_cannot_be_added_twice(env):
    install, _ = env
    install(DB(documents=[{"id": "d1", "course_id": "c1", "media_url": LINK, "deleted_at": None}]))
    with pytest.raises(HTTPException) as e:
        knowledge.add_video("c1", knowledge.VideoAdd(link=LINK), None, {"id": "u"})
    assert e.value.status_code == 409
