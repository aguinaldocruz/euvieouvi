"""Explicit repositories; none of these methods commits a transaction."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from euvieouvi.database.models import (
    Library,
    MediaIdentifier,
    MediaItem,
    Setting,
    Source,
    SourceMediaRef,
    SyncCheckpoint,
    SyncError,
    SyncRun,
    SyncRunLibrary,
    WatchEvent,
    WatchState,
)
from euvieouvi.extensions import Base


class Repository[ModelT: Base]:
    """Small typed repository base using SQLAlchemy 2.x statements."""

    model: type[ModelT]

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, entity: ModelT) -> ModelT:
        self.session.add(entity)
        return entity

    def get(self, entity_id: object) -> ModelT | None:
        return self.session.get(self.model, entity_id)

    def list(self, *, limit: int = 200, offset: int = 0) -> Sequence[ModelT]:
        statement = select(self.model).limit(limit).offset(offset)
        return self.session.scalars(statement).all()

    def one_or_none(self, statement: Select[tuple[ModelT]]) -> ModelT | None:
        return self.session.scalars(statement).one_or_none()


class SourceRepository(Repository[Source]):
    model = Source

    def by_name(self, name: str) -> Source | None:
        return self.one_or_none(select(Source).where(Source.name == name))


class LibraryRepository(Repository[Library]):
    model = Library

    def by_external_identity(self, source_id: int, external_id: str) -> Library | None:
        return self.one_or_none(
            select(Library).where(
                Library.source_id == source_id,
                Library.external_id == external_id,
            )
        )


class MediaItemRepository(Repository[MediaItem]):
    model = MediaItem


class SourceMediaRefRepository(Repository[SourceMediaRef]):
    model = SourceMediaRef

    def by_external_identity(self, source_id: int, external_id: str) -> SourceMediaRef | None:
        return self.one_or_none(
            select(SourceMediaRef).where(
                SourceMediaRef.source_id == source_id,
                SourceMediaRef.external_id == external_id,
            )
        )


class MediaIdentifierRepository(Repository[MediaIdentifier]):
    model = MediaIdentifier


class WatchEventRepository(Repository[WatchEvent]):
    model = WatchEvent

    def by_dedup_key(self, source_id: int, dedup_key: str) -> WatchEvent | None:
        return self.one_or_none(
            select(WatchEvent).where(
                WatchEvent.source_id == source_id,
                WatchEvent.dedup_key == dedup_key,
            )
        )


class WatchStateRepository(Repository[WatchState]):
    model = WatchState

    def by_item_and_source(self, media_item_id: int, source_id: int) -> WatchState | None:
        return self.one_or_none(
            select(WatchState).where(
                WatchState.media_item_id == media_item_id,
                WatchState.source_id == source_id,
            )
        )


class SyncRunRepository(Repository[SyncRun]):
    model = SyncRun


class SyncRunLibraryRepository(Repository[SyncRunLibrary]):
    model = SyncRunLibrary


class SyncCheckpointRepository(Repository[SyncCheckpoint]):
    model = SyncCheckpoint

    def by_library(self, library_id: int) -> SyncCheckpoint | None:
        return self.one_or_none(
            select(SyncCheckpoint).where(SyncCheckpoint.library_id == library_id)
        )


class SyncErrorRepository(Repository[SyncError]):
    model = SyncError


class SettingRepository(Repository[Setting]):
    model = Setting
