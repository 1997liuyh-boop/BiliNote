from sqlalchemy import Boolean, Column, Integer, JSON, String, Text

from app.db.engine import Base


class LibraryNote(Base):
    __tablename__ = "library_notes"

    id = Column(String(128), primary_key=True)
    title = Column(Text, nullable=False, default="未命名笔记")
    created_at = Column(String(40), nullable=False)
    updated_at = Column(String(40), nullable=False)
    status = Column(String(30), nullable=False, default="PENDING")
    category_id = Column(String(36), nullable=True, index=True)
    payload = Column(JSON, nullable=False, default=dict)
    content_hash = Column(String(64), nullable=False, default="")
    archived_hash = Column(String(64), nullable=False, default="")
    archived_path = Column(Text, nullable=False, default="")
    file_stamp = Column(String(80), nullable=False, default="")
    deleted = Column(Boolean, nullable=False, default=False)


class LibraryVersion(Base):
    __tablename__ = "library_versions"

    id = Column(String(64), primary_key=True)
    note_id = Column(String(128), nullable=False, index=True)
    content = Column(Text, nullable=False)
    created_at = Column(String(40), nullable=False)
    style = Column(Text, nullable=False, default="")
    model_name = Column(Text, nullable=False, default="")


class LibraryCategory(Base):
    __tablename__ = "library_categories"

    id = Column(String(36), primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    created_at = Column(String(40), nullable=False)
    updated_at = Column(String(40), nullable=False)
    archived_hash = Column(String(64), nullable=False, default="")
    archived_path = Column(Text, nullable=False, default="")


class ArchiveJob(Base):
    __tablename__ = "library_archive_jobs"

    id = Column(String(36), primary_key=True)
    fingerprint = Column(String(64), nullable=False, unique=True)
    category_id = Column(String(36), nullable=True)
    scope = Column(String(20), nullable=False)
    snapshot = Column(JSON, nullable=False)
    status = Column(String(30), nullable=False, default="QUEUED")
    stage = Column(String(30), nullable=False, default="UPLOAD")
    error = Column(Text, nullable=False, default="")
    result = Column(JSON, nullable=False, default=dict)
    notification = Column(String(30), nullable=False, default="PENDING")
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(String(40), nullable=False)
    updated_at = Column(String(40), nullable=False)
