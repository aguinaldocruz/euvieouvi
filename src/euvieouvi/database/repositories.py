"""Explicit repositories; none of these methods commits a transaction."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from euvieouvi.database.models import (
    Library,
    MediaIdentifier,
    MediaImage,
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

    def enabled_available_for_source(self, source_id: int) -> Sequence[Library]:
        return self.session.scalars(
            select(Library)
            .where(
                Library.source_id == source_id,
                Library.enabled.is_(True),
                Library.available.is_(True),
            )
            .order_by(Library.id)
        ).all()

    def for_source(self, source_id: int) -> Sequence[Library]:
        return self.session.scalars(
            select(Library).where(Library.source_id == source_id).order_by(Library.id)
        ).all()


class MediaItemRepository(Repository[MediaItem]):
    model = MediaItem


class MediaImageRepository(Repository[MediaImage]):
    model = MediaImage

    def by_item_and_type(self, media_item_id: int, image_type: str) -> MediaImage | None:
        return self.one_or_none(
            select(MediaImage).where(
                MediaImage.media_item_id == media_item_id,
                MediaImage.image_type == image_type,
            )
        )


class SourceMediaRefRepository(Repository[SourceMediaRef]):
    model = SourceMediaRef

    def by_external_identity(self, source_id: int, external_id: str) -> SourceMediaRef | None:
        return self.one_or_none(
            select(SourceMediaRef).where(
                SourceMediaRef.source_id == source_id,
                SourceMediaRef.external_id == external_id,
            )
        )

    def for_library(self, library_id: int) -> Sequence[SourceMediaRef]:
        return self.session.scalars(
            select(SourceMediaRef).where(SourceMediaRef.library_id == library_id)
        ).all()


class MediaIdentifierRepository(Repository[MediaIdentifier]):
    model = MediaIdentifier

    def by_identity(
        self, media_item_id: int, provider: str, external_id: str
    ) -> MediaIdentifier | None:
        return self.one_or_none(
            select(MediaIdentifier).where(
                MediaIdentifier.media_item_id == media_item_id,
                MediaIdentifier.provider == provider,
                MediaIdentifier.external_id == external_id,
            )
        )


class WatchEventRepository(Repository[WatchEvent]):
    model = WatchEvent

    def by_dedup_key(self, source_id: int, dedup_key: str) -> WatchEvent | None:
        return self.one_or_none(
            select(WatchEvent).where(
                WatchEvent.source_id == source_id,
                WatchEvent.dedup_key == dedup_key,
            )
        )

    def by_source_event_id(self, source_id: int, source_event_id: str) -> WatchEvent | None:
        return self.one_or_none(
            select(WatchEvent).where(
                WatchEvent.source_id == source_id,
                WatchEvent.source_event_id == source_event_id,
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

    def running(self) -> Sequence[SyncRun]:
        return self.session.scalars(
            select(SyncRun).where(SyncRun.status.in_(["queued", "running"])).order_by(SyncRun.id)
        ).all()


class SyncRunLibraryRepository(Repository[SyncRunLibrary]):
    model = SyncRunLibrary

    def by_run_and_library(self, sync_run_id: int, library_id: int) -> SyncRunLibrary | None:
        return self.one_or_none(
            select(SyncRunLibrary).where(
                SyncRunLibrary.sync_run_id == sync_run_id,
                SyncRunLibrary.library_id == library_id,
            )
        )

    def for_run(self, sync_run_id: int) -> Sequence[SyncRunLibrary]:
        return self.session.scalars(
            select(SyncRunLibrary)
            .where(SyncRunLibrary.sync_run_id == sync_run_id)
            .order_by(SyncRunLibrary.id)
        ).all()


class SyncCheckpointRepository(Repository[SyncCheckpoint]):
    model = SyncCheckpoint

    def by_library(self, library_id: int) -> SyncCheckpoint | None:
        return self.one_or_none(
            select(SyncCheckpoint).where(SyncCheckpoint.library_id == library_id)
        )


class SyncErrorRepository(Repository[SyncError]):
    model = SyncError

    def for_run(self, sync_run_id: int) -> Sequence[SyncError]:
        return self.session.scalars(
            select(SyncError).where(SyncError.sync_run_id == sync_run_id).order_by(SyncError.id)
        ).all()


class SettingRepository(Repository[Setting]):
    model = Setting
